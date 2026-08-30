"""高价值存储结构的只读诊断。

日志诊断只读取文件元数据；SQLite 诊断使用 immutable read-only URI 读取
页统计。两类结果都不能直接进入通用 cleanup executor。
"""
from __future__ import annotations

import fnmatch
import sqlite3
import stat
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from .filesystem import lstat_retry, scandir_entries
from .macos import discover_darwin_user_cache, discover_darwin_user_temp
from .models import FileFacts, FileIdentity, Item, ScanIssue, ScanResult, normalize_path
from .predicates import Predicate, ProtectionGate
from .processes import (
    OpenFileDetectionError,
    OpenFileSnapshot,
    ProcessDetectionError,
    ProcessSnapshot,
    capture_open_file_snapshot,
    capture_process_snapshot,
)
from .updater import UpdaterAssessment, assess_updater_staging_root

RETENTION_DAYS = (7, 14, 30)


@dataclass(frozen=True)
class RetentionRule:
    category: str
    path: str
    process_markers: tuple[str, ...]


@dataclass(frozen=True)
class SQLiteRule:
    category: str
    path: str
    process_markers: tuple[str, ...]
    minimum_free_bytes: int = 1024 * 1024


@dataclass(frozen=True)
class DarwinTransientPattern:
    category: str
    name_globs: tuple[str, ...]
    process_markers: tuple[str, ...] = ()


RETENTION_RULES: tuple[RetentionRule, ...] = (
    RetentionRule(
        "WorkBuddy logs 保留期",
        "~/.workbuddy/logs",
        ("WorkBuddy.app",),
    ),
    RetentionRule(
        "WorkBuddy traces 保留期",
        "~/.workbuddy/traces",
        ("WorkBuddy.app",),
    ),
    RetentionRule(
        "WorkBuddy macOS logs 保留期",
        "~/Library/Logs/WorkBuddy",
        ("WorkBuddy.app",),
    ),
    RetentionRule(
        "WorkBuddy audit log 保留期",
        "~/.workbuddy/audit-log",
        ("WorkBuddy.app",),
    ),
    RetentionRule(
        "Codex macOS logs 保留期",
        "~/Library/Logs/com.openai.codex",
        ("ChatGPT.app", "Codex.app", "codex"),
    ),
    RetentionRule(
        "Codex runtime cache 保留期",
        "~/.cache/codex-runtimes",
        ("ChatGPT.app", "Codex.app", "codex"),
    ),
    RetentionRule(
        "Lark SDK logs 保留期",
        "~/Library/Application Support/LarkShell/sdk_storage/log",
        ("Lark.app", "Feishu", "Lark Helper"),
    ),
    RetentionRule(
        "Shadowrocket logs 保留期",
        (
            "~/Library/Group Containers/"
            "group.com.liguangming.Shadowrocket/Library/Caches/Logs"
        ),
        ("Shadowrocket.app", "MacPacketTunnel", "MacPacket"),
    ),
    RetentionRule(
        "TRAE logs 保留期",
        "~/Library/Application Support/TRAE SOLO CN/logs",
        ("TRAE SOLO CN.app", "TRAE SOLO CN Helper"),
    ),
    RetentionRule(
        "UURemote updater logs 保留期",
        "~/Library/Application Support/com.netease.uuremote.updater/Logs",
        ("UURemote.app", "UURemoteService", "UURemoteServer"),
    ),
    RetentionRule(
        "UURemote application logs 保留期",
        "~/Library/Application Support/com.netease.uuremote/Logs",
        ("UURemote.app", "UURemoteService", "UURemoteServer"),
    ),
    RetentionRule(
        "UURemote 历史安装包保留期",
        "~/Library/Application Support/com.netease.uuremote.updater/download",
        ("UURemote.app", "UURemoteService", "UURemoteServer"),
    ),
)


DARWIN_TEMP_RETENTION_PATTERNS: tuple[DarwinTransientPattern, ...] = (
    DarwinTransientPattern(
        "Darwin Go build 临时目录保留期",
        ("go-build*",),
        ("/bin/go ", "go build", "go test", "gopls"),
    ),
    DarwinTransientPattern(
        "Darwin Node compile cache 保留期",
        ("node-compile-cache", "v8-compile-cache-*"),
        ("node",),
    ),
    DarwinTransientPattern(
        "Qoder CLI 版本化运行时保留期",
        ("qodercli-natives-*",),
        ("qoder", "Qoder"),
    ),
    DarwinTransientPattern(
        "Qoder CLI updater 解压目录保留期",
        ("qoderclicn-update-*-extract",),
        ("qoder", "Qoder"),
    ),
    DarwinTransientPattern(
        "Electron 下载临时目录保留期",
        ("electron-download-*",),
    ),
    DarwinTransientPattern(
        "AI toolhost snapshots 保留期",
        ("toolhost-snapshots", "trae-agent-toolhost-*"),
        ("agent-tool-host", "TRAE SOLO CN.app"),
    ),
    DarwinTransientPattern(
        "UURemote Darwin 临时目录保留期",
        ("UURemote",),
        ("UURemote.app", "UURemoteService", "UURemoteServer"),
    ),
)


DARWIN_CODE_SIGN_RETENTION_PATTERNS: tuple[DarwinTransientPattern, ...] = (
    DarwinTransientPattern(
        "Google Chrome code-sign clone 保留期",
        ("com.google.Chrome.code_sign_clone",),
        ("Google Chrome.app",),
    ),
    DarwinTransientPattern(
        "Comet code-sign clone 保留期",
        ("ai.perplexity.comet.code_sign_clone",),
        ("Comet.app",),
    ),
    DarwinTransientPattern(
        "Doubao browser code-sign clone 保留期",
        ("com.bot.pc.doubao.browser.code_sign_clone",),
        ("Doubao.app", "豆包"),
    ),
    DarwinTransientPattern(
        "应用 code-sign clone 保留期",
        ("*.code_sign_clone",),
    ),
)


_MAX_DYNAMIC_RETENTION_ROOTS = 128


SQLITE_RULES: tuple[SQLiteRule, ...] = (
    SQLiteRule(
        "Codex SQLite 内部空闲页",
        "~/.codex/logs_2.sqlite",
        ("ChatGPT.app", "Codex.app", "codex"),
    ),
)

_QODER_SHIPIT_TEMP_PREFIX = "com.aliyun.lingma.ide.ShipIt."
_QODER_SHIPIT_BUNDLE_ID = "com.aliyun.lingma.ide"
_QODER_PROCESS_MARKERS = ("Qoder CN IDE.app", "Qoder CN IDE Helper")


@dataclass
class _RetentionMeasurement:
    file_count: int = 0
    logical_bytes: int = 0
    allocated_bytes: int = 0
    older_bytes: dict[int, int] | None = None
    newest_mtime: float | None = None
    excluded_paths: int = 0
    cross_device_paths: int = 0
    cloud_file_count: int = 0

    def __post_init__(self) -> None:
        if self.older_bytes is None:
            self.older_bytes = {days: 0 for days in RETENTION_DAYS}


def _knowledge_base_ignores(
    protection: Predicate,
    path: Path,
) -> bool:
    return isinstance(
        protection, ProtectionGate
    ) and protection.knowledge_base_ignores(path)


def _predicates_ignore_after_knowledge_base(
    protection: Predicate,
    facts: FileFacts,
) -> bool:
    if isinstance(protection, ProtectionGate):
        return protection.predicates_ignore(facts)
    return protection.should_ignore(facts)


def _format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    raise AssertionError("unreachable")


def _append_filesystem_issue(
    issues: list[ScanIssue],
    exc: OSError,
    path: Path,
    task: str,
) -> None:
    if isinstance(exc, PermissionError):
        code = "permission_denied"
    elif isinstance(exc, FileNotFoundError):
        code = "path_changed"
    else:
        code = "filesystem_error"
    issues.append(
        ScanIssue(code=code, message=str(exc), task=task, path=path)
    )


def _matching_transient_pattern(
    name: str,
    patterns: Sequence[DarwinTransientPattern],
) -> DarwinTransientPattern | None:
    return next(
        (
            pattern
            for pattern in patterns
            if any(
                fnmatch.fnmatchcase(name, name_glob)
                for name_glob in pattern.name_globs
            )
        ),
        None,
    )


def _discover_transient_rules_under(
    root: Path,
    patterns: Sequence[DarwinTransientPattern],
    issues: list[ScanIssue],
    *,
    task: str,
    remaining: int,
) -> list[RetentionRule]:
    if remaining <= 0:
        return []
    try:
        entries = scandir_entries(root)
        rules: list[RetentionRule] = []
        for entry in entries:
            pattern = _matching_transient_pattern(entry.name, patterns)
            if pattern is None:
                continue
            if len(rules) >= remaining:
                issues.append(
                    ScanIssue(
                        code="diagnostic_limit_reached",
                        message=(
                            "动态保留期候选超过安全上限，"
                            "其余路径未进入本次诊断"
                        ),
                        task=task,
                        path=root,
                        blocking=False,
                    )
                )
                break
            rules.append(
                RetentionRule(
                    pattern.category,
                    entry.path,
                    pattern.process_markers,
                )
            )
    except FileNotFoundError:
        return []
    except OSError as exc:
        _append_filesystem_issue(issues, exc, root, task)
        return []
    return rules


def discover_darwin_transient_retention_rules(
) -> tuple[tuple[RetentionRule, ...], tuple[ScanIssue, ...]]:
    """通过 getconf 动态发现公开命名的 Darwin 临时/运行副本根。"""

    rules: list[RetentionRule] = []
    issues: list[ScanIssue] = []
    temp_discovery = discover_darwin_user_temp()
    issues.extend(temp_discovery.issues)
    for root in temp_discovery.paths:
        rules.extend(
            _discover_transient_rules_under(
                root,
                DARWIN_TEMP_RETENTION_PATTERNS,
                issues,
                task="Darwin 临时目录保留期",
                remaining=_MAX_DYNAMIC_RETENTION_ROOTS - len(rules),
            )
        )

    cache_discovery = discover_darwin_user_cache()
    issues.extend(cache_discovery.issues)
    for cache_root in cache_discovery.paths:
        if cache_root.name != "C":
            issues.append(
                ScanIssue(
                    code="path_discovery_failed",
                    message="DARWIN_USER_CACHE_DIR 不是预期的 C 根",
                    task="Darwin code-sign clone 保留期",
                    path=cache_root,
                    blocking=False,
                )
            )
            continue
        rules.extend(
            _discover_transient_rules_under(
                cache_root.parent / "X",
                DARWIN_CODE_SIGN_RETENTION_PATTERNS,
                issues,
                task="Darwin code-sign clone 保留期",
                remaining=_MAX_DYNAMIC_RETENTION_ROOTS - len(rules),
            )
        )

    unique = {
        normalize_path(rule.path): rule
        for rule in rules
    }
    return (
        tuple(unique[path] for path in sorted(unique, key=str)),
        tuple(issues),
    )


def _measure_retention_root(
    root_facts: FileFacts,
    protection: Predicate,
    issues: list[ScanIssue],
    *,
    task: str,
    now: float,
) -> _RetentionMeasurement:
    assert root_facts.stat is not None
    measurement = _RetentionMeasurement()
    seen: set[tuple[int, int]] = set()
    stack = [root_facts.path]
    while stack:
        directory = stack.pop()
        try:
            entries = scandir_entries(directory)
            for entry in entries:
                path = Path(entry.path)
                if _knowledge_base_ignores(protection, path):
                    measurement.excluded_paths += 1
                    continue
                try:
                    stat_result = lstat_retry(path)
                except OSError as exc:
                    _append_filesystem_issue(issues, exc, path, task)
                    continue
                facts = FileFacts(path=path, stat=stat_result)
                if _predicates_ignore_after_knowledge_base(protection, facts):
                    measurement.excluded_paths += 1
                    continue
                if stat.S_ISLNK(stat_result.st_mode):
                    continue
                if stat_result.st_dev != root_facts.stat.st_dev:
                    measurement.cross_device_paths += 1
                    continue
                if facts.is_probable_cloud_placeholder:
                    measurement.cloud_file_count += 1
                    continue
                if stat.S_ISDIR(stat_result.st_mode):
                    stack.append(path)
                    continue
                if not stat.S_ISREG(stat_result.st_mode):
                    continue
                identity = (stat_result.st_dev, stat_result.st_ino)
                if identity in seen:
                    continue
                seen.add(identity)
                allocated = facts.allocated_size
                measurement.file_count += 1
                measurement.logical_bytes += facts.logical_size
                measurement.allocated_bytes += allocated
                if (
                    measurement.newest_mtime is None
                    or stat_result.st_mtime > measurement.newest_mtime
                ):
                    measurement.newest_mtime = stat_result.st_mtime
                assert measurement.older_bytes is not None
                for days in RETENTION_DAYS:
                    if stat_result.st_mtime <= now - days * 86400:
                        measurement.older_bytes[days] += allocated
        except OSError as exc:
            _append_filesystem_issue(issues, exc, directory, task)
    return measurement


def scan_retention_rules(
    rules: Sequence[RetentionRule],
    protection: Predicate,
    *,
    process_snapshot: ProcessSnapshot | None,
    open_files: OpenFileSnapshot | None,
    now: float | None = None,
) -> ScanResult:
    """按 7/14/30 天报告公开存储根物理占用；始终只读且不可执行。"""

    result = ScanResult()
    observed_at = time.time() if now is None else now
    for rule in rules:
        root = normalize_path(rule.path)
        if _knowledge_base_ignores(protection, root):
            continue
        try:
            root_stat = lstat_retry(root)
        except FileNotFoundError:
            continue
        except OSError as exc:
            _append_filesystem_issue(result.issues, exc, root, rule.category)
            continue
        root_facts = FileFacts(path=root, stat=root_stat)
        if _predicates_ignore_after_knowledge_base(protection, root_facts):
            continue
        if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
            result.issues.append(
                ScanIssue(
                    code="diagnostic_root_invalid",
                    message="保留期诊断根必须是非符号链接目录",
                    task=rule.category,
                    path=root,
                )
            )
            continue
        measurement = _measure_retention_root(
            root_facts,
            protection,
            result.issues,
            task=rule.category,
            now=observed_at,
        )
        if measurement.file_count == 0 and measurement.allocated_bytes == 0:
            continue
        running = (
            process_snapshot.any_running(rule.process_markers)
            if process_snapshot is not None
            else None
        )
        open_handles = open_files.count_under(root) if open_files is not None else None
        assert measurement.older_bytes is not None
        notes = [
            f"{measurement.file_count} 个文件",
            ", ".join(
                f">{days} 天 {_format_bytes(measurement.older_bytes[days])}"
                for days in RETENTION_DAYS
            ),
        ]
        if running:
            notes.append("相关应用或网络扩展正在运行")
        elif running is None:
            notes.append("进程状态未知")
        if open_handles:
            notes.append(f"检测到 {open_handles} 个打开句柄")
        elif open_handles is None:
            notes.append("打开句柄状态未知")
        if measurement.excluded_paths:
            notes.append(f"跳过 {measurement.excluded_paths} 个保护路径")
        if measurement.cross_device_paths:
            notes.append(f"跳过 {measurement.cross_device_paths} 个跨卷路径")
        if measurement.cloud_file_count:
            notes.append(f"跳过 {measurement.cloud_file_count} 个云占位文件")
        latest_mtime = measurement.newest_mtime
        result.items.append(
            Item(
                root,
                measurement.allocated_bytes,
                rule.category,
                "critical",
                "；".join(notes) + "；只读诊断，不提供按期限批量删除执行器",
                logical_size=measurement.logical_bytes,
                allocated_size=measurement.allocated_bytes,
                actionable=False,
                action_block_reason="保留期诊断仅只读",
                identity=FileIdentity.from_stat(root_stat),
                latest_mtime=latest_mtime,
                age_days=(
                    max(0, int((observed_at - latest_mtime) // 86400))
                    if latest_mtime is not None
                    else None
                ),
                preselected=False,
                excluded_paths=measurement.excluded_paths,
                cross_device_paths=measurement.cross_device_paths,
                running_process_markers=rule.process_markers,
                diagnostic_kind="retention",
                open_handle_count=open_handles,
                retention_file_count=measurement.file_count,
                retention_7d_bytes=measurement.older_bytes[7],
                retention_14d_bytes=measurement.older_bytes[14],
                retention_30d_bytes=measurement.older_bytes[30],
            )
        )
    return result


def _capture_runtime_snapshots(
    result: ScanResult,
    *,
    task: str,
) -> tuple[ProcessSnapshot | None, OpenFileSnapshot | None]:
    try:
        processes = capture_process_snapshot()
    except ProcessDetectionError as exc:
        processes = None
        result.issues.append(
            ScanIssue(
                code="process_detection_failed",
                message=str(exc),
                task=task,
                blocking=False,
            )
        )
    try:
        open_files = capture_open_file_snapshot()
    except OpenFileDetectionError as exc:
        open_files = None
        result.issues.append(
            ScanIssue(
                code="open_file_detection_failed",
                message=str(exc),
                task=task,
                blocking=False,
            )
        )
    return processes, open_files


def scan_retention_diagnostics(protection: Predicate) -> ScanResult:
    result = ScanResult()
    dynamic_rules, discovery_issues = discover_darwin_transient_retention_rules()
    result.issues.extend(discovery_issues)
    processes, open_files = _capture_runtime_snapshots(
        result,
        task="日志/runtime 保留期",
    )
    scanned = scan_retention_rules(
        (*RETENTION_RULES, *dynamic_rules),
        protection,
        process_snapshot=processes,
        open_files=open_files,
    )
    result.items.extend(scanned.items)
    result.issues.extend(scanned.issues)
    return result


def scan_darwin_temp_updater_diagnostics(protection: Predicate) -> ScanResult:
    """报告 Darwin temp 中的 Qoder ShipIt 完整应用副本，始终不可执行。"""

    result = ScanResult()
    discovery = discover_darwin_user_temp()
    result.issues.extend(discovery.issues)
    processes, open_files = _capture_runtime_snapshots(
        result,
        task="Darwin updater 临时副本",
    )
    observed_at = time.time()
    for temp_root in discovery.paths:
        try:
            entries = tuple(scandir_entries(temp_root))
        except OSError as exc:
            _append_filesystem_issue(
                result.issues,
                exc,
                temp_root,
                "Darwin updater 临时副本",
            )
            continue
        for entry in entries:
            if not entry.name.startswith(_QODER_SHIPIT_TEMP_PREFIX):
                continue
            path = Path(entry.path)
            if _knowledge_base_ignores(protection, path):
                continue
            try:
                path_stat = lstat_retry(path)
            except OSError as exc:
                _append_filesystem_issue(
                    result.issues,
                    exc,
                    path,
                    "Darwin updater 临时副本",
                )
                continue
            facts = FileFacts(path=path, stat=path_stat)
            if _predicates_ignore_after_knowledge_base(protection, facts):
                continue
            if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISDIR(path_stat.st_mode):
                result.issues.append(
                    ScanIssue(
                        code="diagnostic_root_invalid",
                        message="Darwin updater 临时根必须是非符号链接目录",
                        task="Darwin updater 临时副本",
                        path=path,
                    )
                )
                continue
            measurement = _measure_retention_root(
                facts,
                protection,
                result.issues,
                task="Darwin updater 临时副本",
                now=observed_at,
            )
            if measurement.allocated_bytes == 0:
                continue
            assessment = assess_updater_staging_root(
                path,
                bundle_id=_QODER_SHIPIT_BUNDLE_ID,
                staged_app_globs=("Qoder CN IDE.app",),
            ) or UpdaterAssessment(
                "version_unknown",
                _QODER_SHIPIT_BUNDLE_ID,
            )
            running = (
                processes.any_running(_QODER_PROCESS_MARKERS)
                if processes is not None
                else None
            )
            open_handles = (
                open_files.count_under(path) if open_files is not None else None
            )
            notes = [assessment.note, f"{measurement.file_count} 个文件"]
            if running:
                notes.append("相关应用正在运行")
            elif running is None:
                notes.append("进程状态未知")
            if open_handles:
                notes.append(f"检测到 {open_handles} 个打开句柄")
            elif open_handles is None:
                notes.append("打开句柄状态未知")
            latest_mtime = measurement.newest_mtime
            result.items.append(
                Item(
                    path,
                    measurement.allocated_bytes,
                    "Qoder ShipIt Darwin 临时副本",
                    "critical",
                    "；".join(notes) + "；只读诊断，不自动删除 temp 中的 app",
                    logical_size=measurement.logical_bytes,
                    allocated_size=measurement.allocated_bytes,
                    actionable=False,
                    action_block_reason="Darwin updater 临时副本仅诊断",
                    identity=FileIdentity.from_stat(path_stat),
                    latest_mtime=latest_mtime,
                    age_days=(
                        max(0, int((observed_at - latest_mtime) // 86400))
                        if latest_mtime is not None
                        else None
                    ),
                    preselected=False,
                    excluded_paths=measurement.excluded_paths,
                    cross_device_paths=measurement.cross_device_paths,
                    running_process_markers=_QODER_PROCESS_MARKERS,
                    updater_status=assessment.status,
                    installed_version=assessment.installed_version,
                    staged_version=assessment.staged_version,
                    updater_external_install=assessment.external_install,
                    diagnostic_kind="updater_temp",
                    open_handle_count=open_handles,
                )
            )
    return result


def _sqlite_uri(path: Path) -> str:
    return f"file:{quote(str(path), safe='/')}?mode=ro&immutable=1"


def _sidecar_allocated_bytes(path: Path) -> int:
    total = 0
    for suffix in ("-journal", "-shm", "-wal"):
        sidecar = Path(f"{path}{suffix}")
        try:
            sidecar_stat = lstat_retry(sidecar)
        except FileNotFoundError:
            continue
        except OSError:
            continue
        if stat.S_ISREG(sidecar_stat.st_mode):
            total += getattr(sidecar_stat, "st_blocks", 0) * 512
    return total


def scan_sqlite_rules(
    rules: Sequence[SQLiteRule],
    protection: Predicate,
    *,
    process_snapshot: ProcessSnapshot | None,
    open_files: OpenFileSnapshot | None,
    connector: Callable[..., sqlite3.Connection] = sqlite3.connect,
) -> ScanResult:
    """读取 SQLite 页统计；不创建 WAL/SHM，也不提供 VACUUM。"""

    result = ScanResult()
    for rule in rules:
        path = normalize_path(rule.path)
        if _knowledge_base_ignores(protection, path):
            continue
        try:
            before = lstat_retry(path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            _append_filesystem_issue(result.issues, exc, path, rule.category)
            continue
        facts = FileFacts(path=path, stat=before)
        if _predicates_ignore_after_knowledge_base(protection, facts):
            continue
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            result.issues.append(
                ScanIssue(
                    code="sqlite_diagnostic_invalid",
                    message="SQLite 诊断目标必须是非符号链接普通文件",
                    task=rule.category,
                    path=path,
                    blocking=False,
                )
            )
            continue
        try:
            connection = connector(_sqlite_uri(path), uri=True, timeout=1.0)
            try:
                page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
                page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
                freelist_count = int(
                    connection.execute("PRAGMA freelist_count").fetchone()[0]
                )
            finally:
                connection.close()
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            result.issues.append(
                ScanIssue(
                    code="sqlite_diagnostic_failed",
                    message=f"{type(exc).__name__}: {exc}",
                    task=rule.category,
                    path=path,
                    blocking=False,
                )
            )
            continue
        try:
            after = lstat_retry(path)
        except OSError as exc:
            _append_filesystem_issue(result.issues, exc, path, rule.category)
            continue
        before_snapshot = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        after_snapshot = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        if before_snapshot != after_snapshot:
            result.issues.append(
                ScanIssue(
                    code="sqlite_changed_during_scan",
                    message="数据库在页统计期间发生变化，已丢弃本次结果",
                    task=rule.category,
                    path=path,
                    blocking=False,
                )
            )
            continue
        if page_size <= 0 or page_count < 0 or not 0 <= freelist_count <= page_count:
            result.issues.append(
                ScanIssue(
                    code="sqlite_diagnostic_invalid",
                    message="SQLite 页统计超出有效范围",
                    task=rule.category,
                    path=path,
                    blocking=False,
                )
            )
            continue
        internal_free_bytes = page_size * freelist_count
        if internal_free_bytes < rule.minimum_free_bytes:
            continue
        allocated_bytes = getattr(before, "st_blocks", 0) * 512
        potential_bytes = min(internal_free_bytes, allocated_bytes)
        ratio = freelist_count / page_count if page_count else 0.0
        running = (
            process_snapshot.any_running(rule.process_markers)
            if process_snapshot is not None
            else None
        )
        open_handles = (
            open_files.count_sqlite_family(path) if open_files is not None else None
        )
        wal_bytes = _sidecar_allocated_bytes(path)
        notes = [
            f"数据库 {_format_bytes(before.st_size)}",
            f"内部空闲页 {_format_bytes(internal_free_bytes)} ({ratio:.1%})",
            "immutable read-only PRAGMA",
        ]
        if wal_bytes:
            notes.append(f"WAL/SHM/journal {_format_bytes(wal_bytes)}")
        if running:
            notes.append("相关应用正在运行")
        elif running is None:
            notes.append("进程状态未知")
        if open_handles:
            notes.append(f"检测到 {open_handles} 个数据库家族句柄")
        elif open_handles is None:
            notes.append("打开句柄状态未知")
        result.items.append(
            Item(
                path,
                potential_bytes,
                rule.category,
                "critical",
                "；".join(notes) + "；不自动 VACUUM 或删除数据库",
                logical_size=before.st_size,
                allocated_size=allocated_bytes,
                actionable=False,
                action_block_reason="SQLite 内部空闲页仅诊断，未提供压缩执行器",
                identity=FileIdentity.from_stat(before),
                preselected=False,
                resource_total_size=before.st_size,
                running_process_markers=rule.process_markers,
                diagnostic_kind="sqlite_freelist",
                open_handle_count=open_handles,
                sqlite_page_size=page_size,
                sqlite_page_count=page_count,
                sqlite_freelist_count=freelist_count,
                sqlite_internal_free_bytes=internal_free_bytes,
                sqlite_internal_free_ratio=ratio,
                sqlite_wal_bytes=wal_bytes,
            )
        )
    return result


def scan_sqlite_diagnostics(protection: Predicate) -> ScanResult:
    result = ScanResult()
    processes, open_files = _capture_runtime_snapshots(
        result,
        task="SQLite 内部空闲页",
    )
    scanned = scan_sqlite_rules(
        SQLITE_RULES,
        protection,
        process_snapshot=processes,
        open_files=open_files,
    )
    result.items.extend(scanned.items)
    result.issues.extend(scanned.issues)
    return result
