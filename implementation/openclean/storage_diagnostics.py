"""高价值存储结构的只读诊断。

日志诊断只读取文件元数据；SQLite 诊断使用 immutable read-only URI 读取
页统计。两类结果都不能直接进入通用 cleanup executor。
"""
from __future__ import annotations

import fnmatch
import os
import sqlite3
import stat
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import quote

from .filesystem import lstat_retry, scandir_entries
from .macos import (
    discover_darwin_user_cache,
    discover_darwin_user_temp,
    scan_symlink_anchor,
    symlink_component,
)
from .models import FileFacts, FileIdentity, Item, ScanIssue, ScanResult, normalize_path
from .predicates import Predicate, ProtectionGate
from .processes import (
    DeletedOpenFile,
    DeletedOpenFileSnapshot,
    OpenFileDetectionError,
    OpenFileSnapshot,
    ProcessDetectionError,
    ProcessSnapshot,
    capture_deleted_open_file_snapshot,
    capture_open_file_snapshot,
    capture_process_snapshot,
)
from .updater import UpdaterAssessment, assess_updater_staging_root

RETENTION_DAYS = (7, 14, 30)
_CODEX_PROCESS_MARKERS = ("ChatGPT.app", "Codex.app", "codex")
_CODEX_LOG_CATEGORY = "Codex macOS logs 保留期"
_CODEX_MARKETPLACE_STAGING = "~/.codex/.tmp/marketplaces/.staging"
_CODEX_GIT_TEMP_ROOT = "~/.codex/.tmp"
_CODEX_CRASHPAD_PENDING = (
    "~/Library/Application Support/Codex/Crashpad/pending"
)
_CODEX_LOG_RELATIVE_ROOT = "Library/Logs/com.openai.codex"
_MAX_CODEX_LOG_PARTITIONS = 128
_MAX_CODEX_STAGING_ROOTS = 2048
_MAX_CODEX_GIT_ROOTS = 8192
_MAX_CRASHPAD_ENTRIES = 100_000


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
class BrowserStorageRoot:
    category: str
    relative_root: str
    process_markers: tuple[str, ...]


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
        _CODEX_LOG_CATEGORY,
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


BROWSER_STORAGE_ROOTS: tuple[BrowserStorageRoot, ...] = (
    BrowserStorageRoot(
        "Google Chrome Service Worker CacheStorage 保留期",
        "Library/Application Support/Google/Chrome",
        ("Google Chrome.app", "Google Chrome Helper"),
    ),
    BrowserStorageRoot(
        "Brave Service Worker CacheStorage 保留期",
        "Library/Application Support/BraveSoftware/Brave-Browser",
        ("Brave Browser.app", "Brave Browser Helper"),
    ),
    BrowserStorageRoot(
        "Microsoft Edge Service Worker CacheStorage 保留期",
        "Library/Application Support/Microsoft Edge",
        ("Microsoft Edge.app", "Microsoft Edge Helper"),
    ),
    BrowserStorageRoot(
        "Comet Service Worker CacheStorage 保留期",
        "Library/Application Support/Comet",
        ("Comet.app", "Comet Helper"),
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
_MAX_VOLUME_ROOTS = 128
_MAX_BROWSER_PROFILES = 64


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


def discover_browser_storage_retention_rules(
    *,
    home: Path | None = None,
    roots: Sequence[BrowserStorageRoot] = BROWSER_STORAGE_ROOTS,
) -> tuple[tuple[RetentionRule, ...], tuple[ScanIssue, ...]]:
    """发现已知 Chromium 浏览器的用户 profile CacheStorage 根。"""

    base = normalize_path(home or Path.home())
    rules: list[RetentionRule] = []
    issues: list[ScanIssue] = []
    for browser in roots:
        browser_root = normalize_path(base / browser.relative_root)
        try:
            if symlink_component(browser_root, anchor=base) is not None:
                continue
            browser_stat = lstat_retry(browser_root)
            if stat.S_ISLNK(browser_stat.st_mode) or not stat.S_ISDIR(
                browser_stat.st_mode
            ):
                continue
            entries = sorted(
                scandir_entries(browser_root),
                key=lambda item: item.name,
            )
        except FileNotFoundError:
            continue
        except OSError as exc:
            _append_filesystem_issue(
                issues,
                exc,
                browser_root,
                browser.category,
            )
            continue
        for entry in entries:
            if entry.name != "Default" and not fnmatch.fnmatchcase(
                entry.name, "Profile *"
            ):
                continue
            if len(rules) >= _MAX_BROWSER_PROFILES:
                issues.append(
                    ScanIssue(
                        code="diagnostic_limit_reached",
                        message=(
                            "浏览器 profile 数量超过安全上限，"
                            "其余 profile 未进入本次诊断"
                        ),
                        task="浏览器 CacheStorage 保留期",
                        path=browser_root,
                        blocking=False,
                    )
                )
                return tuple(rules), tuple(issues)
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            cache_storage = (
                Path(entry.path) / "Service Worker" / "CacheStorage"
            )
            try:
                if (
                    symlink_component(cache_storage, anchor=browser_root)
                    is not None
                ):
                    continue
                cache_stat = lstat_retry(cache_storage)
            except FileNotFoundError:
                continue
            except OSError as exc:
                _append_filesystem_issue(
                    issues,
                    exc,
                    cache_storage,
                    browser.category,
                )
                continue
            if stat.S_ISLNK(cache_stat.st_mode) or not stat.S_ISDIR(
                cache_stat.st_mode
            ):
                continue
            rules.append(
                RetentionRule(
                    browser.category,
                    str(cache_storage),
                    browser.process_markers,
                )
            )
    return tuple(rules), tuple(issues)


def discover_codex_log_partition_rules(
    *,
    home: Path | None = None,
    limit: int = _MAX_CODEX_LOG_PARTITIONS,
) -> tuple[tuple[RetentionRule, ...], tuple[ScanIssue, ...]]:
    """发现 Codex ``YYYY/MM/DD`` 日志分区，不把日期当成删除策略。"""

    base = normalize_path(home or Path.home())
    root = normalize_path(base / _CODEX_LOG_RELATIVE_ROOT)
    issues: list[ScanIssue] = []
    try:
        if symlink_component(root, anchor=base) is not None:
            return (), ()
        root_stat = lstat_retry(root)
        if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
            return (), ()
        year_entries = sorted(scandir_entries(root), key=lambda item: item.name)
    except FileNotFoundError:
        return (), ()
    except OSError as exc:
        _append_filesystem_issue(issues, exc, root, "Codex 日志日期分区")
        return (), tuple(issues)

    rules: list[RetentionRule] = []
    for year_entry in year_entries:
        if len(year_entry.name) != 4 or not year_entry.name.isdigit():
            continue
        try:
            if not year_entry.is_dir(follow_symlinks=False):
                continue
            month_entries = sorted(
                scandir_entries(Path(year_entry.path)),
                key=lambda item: item.name,
            )
        except OSError:
            continue
        for month_entry in month_entries:
            if len(month_entry.name) != 2 or not month_entry.name.isdigit():
                continue
            try:
                if not month_entry.is_dir(follow_symlinks=False):
                    continue
                day_entries = sorted(
                    scandir_entries(Path(month_entry.path)),
                    key=lambda item: item.name,
                )
            except OSError:
                continue
            for day_entry in day_entries:
                if len(day_entry.name) != 2 or not day_entry.name.isdigit():
                    continue
                try:
                    partition_date = date(
                        int(year_entry.name),
                        int(month_entry.name),
                        int(day_entry.name),
                    )
                    if not day_entry.is_dir(follow_symlinks=False):
                        continue
                except (OSError, ValueError):
                    continue
                if len(rules) >= limit:
                    issues.append(
                        ScanIssue(
                            code="diagnostic_limit_reached",
                            message=(
                                "Codex 日志日期分区超过安全上限，"
                                "其余分区未进入本次诊断"
                            ),
                            task="Codex 日志日期分区",
                            path=root,
                            blocking=False,
                        )
                    )
                    return tuple(rules), tuple(issues)
                rules.append(
                    RetentionRule(
                        f"Codex macOS logs {partition_date.isoformat()}",
                        day_entry.path,
                        _CODEX_PROCESS_MARKERS,
                    )
                )
    return tuple(rules), tuple(issues)


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
        component = symlink_component(root, anchor=scan_symlink_anchor(root))
        if component is not None:
            result.issues.append(
                ScanIssue(
                    code="unsafe_symlink_ancestor",
                    message="保留期诊断根包含符号链接组件，已拒绝扫描",
                    task=rule.category,
                    path=component,
                    blocking=False,
                )
            )
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
    browser_rules, browser_issues = discover_browser_storage_retention_rules()
    result.issues.extend(browser_issues)
    codex_log_rules, codex_log_issues = discover_codex_log_partition_rules()
    result.issues.extend(codex_log_issues)
    processes, open_files = _capture_runtime_snapshots(
        result,
        task="日志/runtime 保留期",
    )
    scanned = scan_retention_rules(
        (
            *RETENTION_RULES,
            *codex_log_rules,
            *browser_rules,
            *dynamic_rules,
        ),
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


def _diagnostic_directory_facts(
    root: Path,
    protection: Predicate,
    issues: list[ScanIssue],
    *,
    task: str,
    anchor: Path | None = None,
) -> FileFacts | None:
    if _knowledge_base_ignores(protection, root):
        return None
    if anchor is not None and symlink_component(root, anchor=anchor) is not None:
        issues.append(
            ScanIssue(
                code="diagnostic_root_invalid",
                message="结构诊断根包含符号链接路径组件",
                task=task,
                path=root,
                blocking=False,
            )
        )
        return None
    component = symlink_component(root, anchor=scan_symlink_anchor(root))
    if component is not None:
        issues.append(
            ScanIssue(
                code="unsafe_symlink_ancestor",
                message="结构诊断根包含符号链接组件，已拒绝扫描",
                task=task,
                path=component,
                blocking=False,
            )
        )
        return None
    try:
        root_stat = lstat_retry(root)
    except FileNotFoundError:
        return None
    except OSError as exc:
        _append_filesystem_issue(issues, exc, root, task)
        return None
    facts = FileFacts(path=root, stat=root_stat)
    if _predicates_ignore_after_knowledge_base(protection, facts):
        return None
    if (
        stat.S_ISLNK(root_stat.st_mode)
        or not stat.S_ISDIR(root_stat.st_mode)
        or facts.is_probable_cloud_placeholder
    ):
        issues.append(
            ScanIssue(
                code="diagnostic_root_invalid",
                message="结构诊断根必须是本地非符号链接目录",
                task=task,
                path=root,
                blocking=False,
            )
        )
        return None
    return facts


def _runtime_diagnostic_notes(
    process_snapshot: ProcessSnapshot | None,
    markers: tuple[str, ...],
    open_handles: int | None,
) -> list[str]:
    notes: list[str] = []
    if process_snapshot is None:
        notes.append("进程状态未知")
    elif process_snapshot.any_running(markers):
        notes.append("相关 Codex 进程正在运行")
    if open_handles is None:
        notes.append("打开句柄状态未知")
    elif open_handles:
        notes.append(f"检测到 {open_handles} 个打开句柄")
    return notes


def _candidate_directory_facts(
    path: Path,
    root_device: int,
    protection: Predicate,
    issues: list[ScanIssue],
    *,
    task: str,
) -> FileFacts | None:
    if _knowledge_base_ignores(protection, path):
        return None
    try:
        path_stat = lstat_retry(path)
    except OSError as exc:
        _append_filesystem_issue(issues, exc, path, task)
        return None
    facts = FileFacts(path=path, stat=path_stat)
    if _predicates_ignore_after_knowledge_base(protection, facts):
        return None
    if (
        stat.S_ISLNK(path_stat.st_mode)
        or not stat.S_ISDIR(path_stat.st_mode)
        or facts.is_probable_cloud_placeholder
        or path_stat.st_dev != root_device
    ):
        return None
    return facts


def _candidate_measurement(
    path: Path,
    root_device: int,
    protection: Predicate,
    issues: list[ScanIssue],
    *,
    task: str,
    now: float,
) -> _RetentionMeasurement | None:
    facts = _candidate_directory_facts(
        path,
        root_device,
        protection,
        issues,
        task=task,
    )
    if facts is None:
        return None
    return _measure_retention_root(
        facts,
        protection,
        issues,
        task=task,
        now=now,
    )


def scan_codex_marketplace_staging(
    root: Path,
    protection: Predicate,
    *,
    process_snapshot: ProcessSnapshot | None,
    open_files: OpenFileSnapshot | None,
    now: float | None = None,
    anchor: Path | None = None,
) -> ScanResult:
    """聚合 Codex marketplace 升级 staging；不处理 marketplace 本体。"""

    result = ScanResult()
    task = "Codex marketplace 升级 staging"
    root = normalize_path(root)
    root_facts = _diagnostic_directory_facts(
        root,
        protection,
        result.issues,
        task=task,
        anchor=anchor,
    )
    if root_facts is None or root_facts.stat is None:
        return result
    try:
        entries = sorted(scandir_entries(root), key=lambda item: item.name)
    except OSError as exc:
        _append_filesystem_issue(result.issues, exc, root, task)
        return result
    matching = []
    for entry in entries:
        if not fnmatch.fnmatchcase(entry.name, "marketplace-upgrade-*"):
            continue
        try:
            if entry.is_dir(follow_symlinks=False):
                matching.append(entry)
        except OSError:
            continue
    matched_count = len(matching)
    limit_reached = matched_count > _MAX_CODEX_STAGING_ROOTS
    candidates = matching[:_MAX_CODEX_STAGING_ROOTS]
    if limit_reached:
        result.issues.append(
            ScanIssue(
                code="diagnostic_limit_reached",
                message=(
                    "marketplace staging 数量超过安全上限，"
                    "容量仅包含有界的部分测量"
                ),
                task=task,
                path=root,
            )
        )

    observed_at = time.time() if now is None else now
    count = logical_bytes = allocated_bytes = 0
    open_handles = 0 if open_files is not None else None
    newest_mtime: float | None = None
    excluded_paths = cross_device_paths = cloud_file_count = 0
    for entry in candidates:
        path = Path(entry.path)
        measurement = _candidate_measurement(
            path,
            root_facts.stat.st_dev,
            protection,
            result.issues,
            task=task,
            now=observed_at,
        )
        if measurement is None:
            continue
        count += 1
        logical_bytes += measurement.logical_bytes
        allocated_bytes += measurement.allocated_bytes
        excluded_paths += measurement.excluded_paths
        cross_device_paths += measurement.cross_device_paths
        cloud_file_count += measurement.cloud_file_count
        if (
            measurement.newest_mtime is not None
            and (
                newest_mtime is None
                or measurement.newest_mtime > newest_mtime
            )
        ):
            newest_mtime = measurement.newest_mtime
        if open_files is not None:
            candidate_handles = open_files.count_under(path)
            assert open_handles is not None
            open_handles += candidate_handles
    if count == 0 and not limit_reached:
        return result

    total_count = matched_count if limit_reached else count
    measurement_complete = not limit_reached
    notes = [f"{total_count} 个 marketplace-upgrade-* 目录"]
    if limit_reached:
        notes.append(
            f"有界前缀中完成 {count} 个目录测量，容量为部分结果"
        )
    notes.extend(
        _runtime_diagnostic_notes(
            process_snapshot,
            _CODEX_PROCESS_MARKERS,
            open_handles,
        )
    )
    notes.append("只报告 .staging 直接子项，不包含已安装 marketplace")
    result.items.append(
        Item(
            root,
            allocated_bytes,
            task,
            "critical",
            "；".join(notes),
            logical_size=logical_bytes,
            allocated_size=allocated_bytes,
            actionable=False,
            action_block_reason="Codex marketplace staging 仅诊断",
            identity=FileIdentity.from_stat(root_facts.stat),
            latest_mtime=newest_mtime,
            age_days=(
                max(0, int((observed_at - newest_mtime) // 86400))
                if newest_mtime is not None
                else None
            ),
            preselected=False,
            resource_kind="filesystem_subset",
            excluded_paths=excluded_paths,
            cross_device_paths=cross_device_paths,
            cloud_file_count=cloud_file_count,
            total_count=total_count,
            measured_count=count,
            measurement_complete=measurement_complete,
            running_process_markers=_CODEX_PROCESS_MARKERS,
            diagnostic_kind="codex_transient",
            open_handle_count=open_handles,
        )
    )
    return result


def _directory_contains_only_ds_store(path: Path) -> bool:
    try:
        entries = scandir_entries(path)
    except OSError:
        return False
    for entry in entries:
        if entry.name != ".DS_Store":
            return False
        try:
            entry_stat = lstat_retry(Path(entry.path))
        except OSError:
            return False
        if stat.S_ISLNK(entry_stat.st_mode) or not stat.S_ISREG(
            entry_stat.st_mode
        ):
            return False
    return True


def _is_codex_git_skeleton(path: Path) -> bool:
    try:
        entries = {entry.name: entry for entry in scandir_entries(path)}
    except OSError:
        return False
    required = {"HEAD", "objects", "refs"}
    if not required.issubset(entries) or not set(entries).issubset(
        {*required, ".DS_Store"}
    ):
        return False
    if ".DS_Store" in entries:
        try:
            metadata_stat = lstat_retry(path / ".DS_Store")
        except OSError:
            return False
        if stat.S_ISLNK(metadata_stat.st_mode) or not stat.S_ISREG(
            metadata_stat.st_mode
        ):
            return False
    try:
        head_stat = lstat_retry(path / "HEAD")
        objects_stat = lstat_retry(path / "objects")
        refs_stat = lstat_retry(path / "refs")
        ds_store_stat = (
            lstat_retry(path / ".DS_Store")
            if ".DS_Store" in entries
            else None
        )
    except OSError:
        return False
    if (
        stat.S_ISLNK(head_stat.st_mode)
        or not stat.S_ISREG(head_stat.st_mode)
        or head_stat.st_size > 64
        or stat.S_ISLNK(objects_stat.st_mode)
        or not stat.S_ISDIR(objects_stat.st_mode)
        or stat.S_ISLNK(refs_stat.st_mode)
        or not stat.S_ISDIR(refs_stat.st_mode)
        or (
            ds_store_stat is not None
            and (
                stat.S_ISLNK(ds_store_stat.st_mode)
                or not stat.S_ISREG(ds_store_stat.st_mode)
            )
        )
    ):
        return False
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if not no_follow:
        return False
    flags = os.O_RDONLY | no_follow | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path / "HEAD", flags)
        try:
            head = os.read(descriptor, 65)
        finally:
            os.close(descriptor)
    except OSError:
        return False
    if head not in {b"ref: refs/heads/main", b"ref: refs/heads/main\n"}:
        return False
    return _directory_contains_only_ds_store(
        path / "objects"
    ) and _directory_contains_only_ds_store(path / "refs")


def scan_codex_git_skeletons(
    root: Path,
    protection: Predicate,
    *,
    process_snapshot: ProcessSnapshot | None,
    open_files: OpenFileSnapshot | None,
    now: float | None = None,
    anchor: Path | None = None,
) -> ScanResult:
    """识别 Codex 临时根中的空 Git 骨架，不泛化到普通 Git 仓库。"""

    result = ScanResult()
    task = "Codex Git 临时空壳"
    root = normalize_path(root)
    root_facts = _diagnostic_directory_facts(
        root,
        protection,
        result.issues,
        task=task,
        anchor=anchor,
    )
    if root_facts is None or root_facts.stat is None:
        return result
    try:
        entries = sorted(scandir_entries(root), key=lambda item: item.name)
    except OSError as exc:
        _append_filesystem_issue(result.issues, exc, root, task)
        return result
    matching = [entry for entry in entries if entry.name.startswith("git-")]
    if len(matching) > _MAX_CODEX_GIT_ROOTS:
        result.issues.append(
            ScanIssue(
                code="diagnostic_limit_reached",
                message="Codex git-* 数量超过安全上限，已放弃本次汇总",
                task=task,
                path=root,
                blocking=False,
            )
        )
        return result

    observed_at = time.time() if now is None else now
    count = logical_bytes = allocated_bytes = 0
    open_handles = 0 if open_files is not None else None
    newest_mtime: float | None = None
    excluded_paths = cross_device_paths = cloud_file_count = 0
    for entry in matching:
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        path = Path(entry.path)
        candidate_facts = _candidate_directory_facts(
            path,
            root_facts.stat.st_dev,
            protection,
            result.issues,
            task=task,
        )
        if candidate_facts is None:
            continue
        if not _is_codex_git_skeleton(path):
            continue
        measurement = _measure_retention_root(
            candidate_facts,
            protection,
            result.issues,
            task=task,
            now=observed_at,
        )
        if measurement is None:
            continue
        if not _is_codex_git_skeleton(path):
            continue
        try:
            rechecked_stat = lstat_retry(path)
        except OSError:
            continue
        if (
            stat.S_ISLNK(rechecked_stat.st_mode)
            or not stat.S_ISDIR(rechecked_stat.st_mode)
            or FileIdentity.from_stat(rechecked_stat) != candidate_facts.identity
        ):
            continue
        count += 1
        logical_bytes += measurement.logical_bytes
        allocated_bytes += measurement.allocated_bytes
        excluded_paths += measurement.excluded_paths
        cross_device_paths += measurement.cross_device_paths
        cloud_file_count += measurement.cloud_file_count
        if (
            measurement.newest_mtime is not None
            and (
                newest_mtime is None
                or measurement.newest_mtime > newest_mtime
            )
        ):
            newest_mtime = measurement.newest_mtime
        if open_files is not None:
            candidate_handles = open_files.count_under(path)
            assert open_handles is not None
            open_handles += candidate_handles
    if count == 0:
        return result

    notes = [
        f"{count} 个仅含 HEAD/objects/refs 的 git-* 目录",
        "未匹配普通、bare、worktree、partial clone 或含对象的仓库",
    ]
    notes.extend(
        _runtime_diagnostic_notes(
            process_snapshot,
            _CODEX_PROCESS_MARKERS,
            open_handles,
        )
    )
    result.items.append(
        Item(
            root,
            allocated_bytes,
            task,
            "critical",
            "；".join(notes),
            logical_size=logical_bytes,
            allocated_size=allocated_bytes,
            actionable=False,
            action_block_reason="Codex Git 临时空壳仅诊断",
            identity=FileIdentity.from_stat(root_facts.stat),
            latest_mtime=newest_mtime,
            age_days=(
                max(0, int((observed_at - newest_mtime) // 86400))
                if newest_mtime is not None
                else None
            ),
            preselected=False,
            resource_kind="filesystem_subset",
            excluded_paths=excluded_paths,
            cross_device_paths=cross_device_paths,
            cloud_file_count=cloud_file_count,
            total_count=count,
            running_process_markers=_CODEX_PROCESS_MARKERS,
            diagnostic_kind="codex_transient",
            open_handle_count=open_handles,
        )
    )
    return result


def scan_crashpad_orphan_sidecars(
    root: Path,
    protection: Predicate,
    *,
    process_snapshot: ProcessSnapshot | None,
    open_files: OpenFileSnapshot | None,
    now: float | None = None,
    anchor: Path | None = None,
) -> ScanResult:
    """按文件名关系汇总 Crashpad 孤立 sidecar；不触碰配对 dump。"""

    result = ScanResult()
    task = "Codex Crashpad 孤立 sidecar"
    root = normalize_path(root)
    root_facts = _diagnostic_directory_facts(
        root,
        protection,
        result.issues,
        task=task,
        anchor=anchor,
    )
    if root_facts is None or root_facts.stat is None:
        return result
    try:
        entries = sorted(scandir_entries(root), key=lambda item: item.name)
    except OSError as exc:
        _append_filesystem_issue(result.issues, exc, root, task)
        return result
    if len(entries) > _MAX_CRASHPAD_ENTRIES:
        result.issues.append(
            ScanIssue(
                code="diagnostic_limit_reached",
                message="Crashpad pending 文件数超过安全上限，已放弃本次汇总",
                task=task,
                path=root,
                blocking=False,
            )
        )
        return result

    names = {entry.name for entry in entries}
    observed_at = time.time() if now is None else now
    paired_count = orphan_count = recent_count = 0
    logical_bytes = allocated_bytes = 0
    open_handles = 0 if open_files is not None else None
    newest_mtime: float | None = None
    seen: set[tuple[int, int]] = set()
    for entry in entries:
        if not entry.name.endswith("_sidecar.json"):
            continue
        stem = entry.name[: -len("_sidecar.json")]
        if f"{stem}.dmp" in names:
            paired_count += 1
            continue
        dump_path = root / f"{stem}.dmp"
        try:
            dump_stat = lstat_retry(dump_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            _append_filesystem_issue(result.issues, exc, dump_path, task)
            continue
        else:
            if stat.S_ISREG(dump_stat.st_mode) and not stat.S_ISLNK(
                dump_stat.st_mode
            ):
                paired_count += 1
            continue
        path = Path(entry.path)
        if _knowledge_base_ignores(protection, path):
            continue
        try:
            path_stat = lstat_retry(path)
        except OSError as exc:
            _append_filesystem_issue(result.issues, exc, path, task)
            continue
        facts = FileFacts(path=path, stat=path_stat)
        if (
            _predicates_ignore_after_knowledge_base(protection, facts)
            or stat.S_ISLNK(path_stat.st_mode)
            or not stat.S_ISREG(path_stat.st_mode)
            or path_stat.st_dev != root_facts.stat.st_dev
            or facts.is_probable_cloud_placeholder
        ):
            continue
        identity = (path_stat.st_dev, path_stat.st_ino)
        if identity in seen:
            continue
        seen.add(identity)
        orphan_count += 1
        logical_bytes += facts.logical_size
        allocated_bytes += facts.allocated_size
        recent_count += int(path_stat.st_mtime > observed_at - 86400)
        if newest_mtime is None or path_stat.st_mtime > newest_mtime:
            newest_mtime = path_stat.st_mtime
        if open_files is not None:
            assert open_handles is not None
            open_handles += open_files.count_under(path)
    if orphan_count == 0:
        return result

    notes = [
        f"{orphan_count} 个无同名 .dmp 的 sidecar",
        f"保留 {paired_count} 个与 .dmp 配对的 sidecar",
    ]
    if recent_count:
        notes.append(f"其中 {recent_count} 个在 24 小时内更新")
    notes.extend(
        _runtime_diagnostic_notes(
            process_snapshot,
            _CODEX_PROCESS_MARKERS,
            open_handles,
        )
    )
    notes.append("仅按文件名和 metadata 配对，不读取崩溃内容")
    result.items.append(
        Item(
            root,
            allocated_bytes,
            task,
            "critical",
            "；".join(notes),
            logical_size=logical_bytes,
            allocated_size=allocated_bytes,
            actionable=False,
            action_block_reason="Crashpad 配对诊断不提供删除执行器",
            identity=FileIdentity.from_stat(root_facts.stat),
            latest_mtime=newest_mtime,
            age_days=(
                max(0, int((observed_at - newest_mtime) // 86400))
                if newest_mtime is not None
                else None
            ),
            preselected=False,
            resource_kind="filesystem_subset",
            total_count=orphan_count,
            paired_artifact_count=paired_count,
            recent_artifact_count=recent_count,
            running_process_markers=_CODEX_PROCESS_MARKERS,
            diagnostic_kind="crashpad_pairing",
            open_handle_count=open_handles,
        )
    )
    return result


def scan_codex_storage_artifact_diagnostics(
    protection: Predicate,
) -> ScanResult:
    """汇总 Codex 精确临时结构和 Crashpad 配对关系。"""

    result = ScanResult()
    processes, open_files = _capture_runtime_snapshots(
        result,
        task="Codex 临时结构",
    )
    home = normalize_path(Path.home())
    scans = (
        scan_codex_marketplace_staging(
            normalize_path(_CODEX_MARKETPLACE_STAGING),
            protection,
            process_snapshot=processes,
            open_files=open_files,
            anchor=home,
        ),
        scan_codex_git_skeletons(
            normalize_path(_CODEX_GIT_TEMP_ROOT),
            protection,
            process_snapshot=processes,
            open_files=open_files,
            anchor=home,
        ),
        scan_crashpad_orphan_sidecars(
            normalize_path(_CODEX_CRASHPAD_PENDING),
            protection,
            process_snapshot=processes,
            open_files=open_files,
            anchor=home,
        ),
    )
    for scanned in scans:
        result.items.extend(scanned.items)
        result.issues.extend(scanned.issues)
    return result


def discover_mounted_volume_roots(
) -> tuple[tuple[Path, ...], tuple[ScanIssue, ...]]:
    """发现当前已挂载卷根，不读取卷内文件。"""

    candidates = [Path("/System/Volumes/Data"), Path("/")]
    issues: list[ScanIssue] = []
    for parent in (Path("/System/Volumes"), Path("/Volumes")):
        try:
            entries = tuple(scandir_entries(parent))
        except FileNotFoundError:
            continue
        except OSError as exc:
            _append_filesystem_issue(
                issues,
                exc,
                parent,
                "open-unlinked volume discovery",
            )
            continue
        for entry in entries:
            if len(candidates) >= _MAX_VOLUME_ROOTS:
                issues.append(
                    ScanIssue(
                        code="diagnostic_limit_reached",
                        message="挂载卷数量超过安全上限，其余卷未进入本次诊断",
                        task="open-unlinked volume discovery",
                        path=parent,
                        blocking=False,
                    )
                )
                break
            try:
                if entry.is_dir(follow_symlinks=False) and os.path.ismount(
                    entry.path
                ):
                    candidates.append(Path(entry.path))
            except OSError:
                continue

    roots: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        normalized = normalize_path(candidate)
        if normalized in seen or not os.path.ismount(normalized):
            continue
        seen.add(normalized)
        roots.append(normalized)
    return tuple(roots), tuple(issues)


def scan_open_unlinked_snapshot(
    snapshot: DeletedOpenFileSnapshot,
    *,
    volume_roots: Sequence[Path],
    protection: Predicate | None = None,
) -> ScanResult:
    """按卷汇总 deleted-open 文件；始终只读且不可执行。"""

    result = ScanResult()
    roots_by_device: dict[int, tuple[Path, os.stat_result]] = {}
    for root in volume_roots:
        normalized = normalize_path(root)
        if protection is not None and _knowledge_base_ignores(
            protection, normalized
        ):
            continue
        try:
            root_stat = lstat_retry(normalized)
        except OSError as exc:
            _append_filesystem_issue(
                result.issues,
                exc,
                normalized,
                "open-unlinked volume mapping",
            )
            continue
        if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
            continue
        if protection is not None and _predicates_ignore_after_knowledge_base(
            protection,
            FileFacts(path=normalized, stat=root_stat),
        ):
            continue
        roots_by_device.setdefault(root_stat.st_dev, (normalized, root_stat))

    files_by_device: dict[int, list[DeletedOpenFile]] = {}
    path_unavailable_count = 0
    for deleted_file in snapshot.files:
        if protection is not None:
            if deleted_file.paths and any(
                protection.should_ignore(
                    FileFacts(path=normalize_path(path), stat=None)
                )
                for path in deleted_file.paths
            ):
                continue
            if (
                not deleted_file.paths
                and isinstance(protection, ProtectionGate)
                and protection.has_active_filters
            ):
                path_unavailable_count += 1
                continue
        files_by_device.setdefault(deleted_file.device, []).append(deleted_file)
    if path_unavailable_count:
        result.issues.append(
            ScanIssue(
                code="open_unlinked_path_unavailable",
                message=(
                    f"{path_unavailable_count} 个 deleted-open 文件缺少可用于"
                    "保护规则判定的路径，已按 fail-closed 跳过"
                ),
                task="已删除但仍打开的文件",
            )
        )

    for device, deleted_files in sorted(files_by_device.items()):
        mapped = roots_by_device.get(device)
        if mapped is None:
            result.issues.append(
                ScanIssue(
                    code="volume_mapping_failed",
                    message=(
                        f"无法把 device {device} 的 deleted-open 文件映射到挂载卷"
                    ),
                    task="已删除但仍打开的文件",
                    blocking=False,
                )
            )
            continue
        mount_point, root_stat = mapped
        logical_size = sum(item.logical_size for item in deleted_files)
        if logical_size <= 0:
            continue
        command_stats: dict[str, list[int]] = {}
        for deleted_file in deleted_files:
            for command in deleted_file.commands:
                stats = command_stats.setdefault(command, [0, 0])
                stats[0] += 1
                stats[1] += deleted_file.logical_size
        top_commands = sorted(
            command_stats.items(),
            key=lambda item: (-item[1][1], -item[1][0], item[0]),
        )[:5]
        process_note = (
            "；关联逻辑大小（进程间可重复） "
            + ", ".join(
                f"{command}({_format_bytes(stats[1])})"
                for command, stats in top_commands
            )
            if top_commands
            else ""
        )
        handle_count = sum(item.handle_count for item in deleted_files)
        result.items.append(
            Item(
                mount_point,
                0,
                "已删除但仍打开的文件",
                "critical",
                (
                    f"{len(deleted_files)} 个 device/inode 唯一文件，"
                    f"{handle_count} 个打开记录，逻辑大小上限 "
                    f"{_format_bytes(logical_size)}{process_note}；"
                    "lsof 仅提供逻辑大小上限，APFS 实际可释放块可能更少；"
                    "退出相关应用或重启后由系统释放"
                ),
                logical_size=logical_size,
                allocated_size=None,
                actionable=False,
                action_block_reason="文件仍由进程打开，只能退出应用或重启释放",
                identity=FileIdentity.from_stat(root_stat),
                preselected=False,
                resource_kind="filesystem_subset",
                total_count=len(deleted_files),
                related_process_count=len(command_stats),
                diagnostic_kind="open_unlinked",
                open_handle_count=handle_count,
            )
        )
    return result


def scan_open_unlinked_diagnostics(protection: Predicate) -> ScanResult:
    result = ScanResult()
    try:
        snapshot = capture_deleted_open_file_snapshot()
    except OpenFileDetectionError as exc:
        result.issues.append(
            ScanIssue(
                code="open_unlinked_detection_failed",
                message=str(exc),
                task="已删除但仍打开的文件",
                blocking=False,
            )
        )
        return result
    volume_roots, discovery_issues = discover_mounted_volume_roots()
    result.issues.extend(discovery_issues)
    scanned = scan_open_unlinked_snapshot(
        snapshot,
        volume_roots=volume_roots,
        protection=protection,
    )
    result.items.extend(scanned.items)
    result.issues.extend(scanned.issues)
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
