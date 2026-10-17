"""Codex 临时对象的精确候选与执行复核；不把临时根目录当作垃圾。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
import tomllib
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .macos import filesystem_relative_parts
from .models import FileFacts, FileIdentity, Item, ScanIssue, ScanResult, normalize_path
from .predicates import Predicate, ProtectionGate
from .processes import OpenFileSnapshot, ProcessSnapshot
from .resource_walk import open_directory, open_resource

PROCESS_MARKERS = ("ChatGPT.app", "Codex.app", "codex")
MINIMUM_AGE_DAYS = {"git": 1, "marketplace": 7}
MAX_TARGETS = {"git": 32768, "marketplace": 2048}
_MAX_ENTRIES = 20000
_MAX_ROOT_ENTRIES = 65536
_MAX_JSON_BYTES = 1024 * 1024
_MAX_COMPARE_BYTES = 64 * 1024 * 1024
_METADATA = ".codex-marketplace-install.json"
# Codex core-plugins/marketplace.rs 的发现顺序；未知布局保持只读。
_MANIFESTS = (
    ".agents/plugins/marketplace.json", ".agents/plugins/api_marketplace.json",
    ".claude-plugin/marketplace.json", ".cursor-plugin/marketplace.json",
)
_TASKS = {"git": "Codex Git 临时空骨架", "marketplace": "Codex marketplace 升级暂存"}


class ArtifactBlocked(OSError):
    """内容未知、检查不完整或生命周期条件不满足；消息不含配置正文。"""

    def __init__(self, message: str, *, code: str = "structure_unsupported"):
        super().__init__(message)
        self.code = code


class ArtifactIncomplete(ArtifactBlocked):
    """读取预算、状态探测或证据错误造成检查不完整。"""


def _stamp(value: os.stat_result) -> tuple[int, ...]:
    return (value.st_dev, value.st_ino, value.st_uid, value.st_mode,
            value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def transient_relation(path: Path, home: Path, probe: Callable) -> str:
    """独立于 Item 标记发现受保护范围，包含父容器与内部叶文件。"""
    root = home / ".codex/.tmp"
    relative = filesystem_relative_parts(path, root, probe=probe)
    if relative is not None:
        if not relative:
            return "container"
        if relative[0].casefold().startswith("git-"):
            return "git" if len(relative) == 1 else "container"
        if relative[0].casefold() == "marketplaces":
            if (len(relative) == 3 and relative[1].casefold() == ".staging"
                    and relative[2].casefold().startswith("marketplace-upgrade-")):
                return "marketplace"
            return "container"
    if (filesystem_relative_parts(root, path, probe=probe) is not None
            and probe(root) is not None):
        return "container"
    return ""


@dataclass
class _Tree:
    root: Path
    entries: dict[str, os.stat_result] = field(default_factory=dict)
    logical: int = 0
    allocated: int = 0
    newest_mtime: float = 0
    newest_change: float = 0


class CodexArtifactContext:
    """单次扫描共享配置/安装证据；每次执行复核创建新上下文。"""

    def __init__(self, home: Path, protection: Predicate, *,
                 process_snapshot: ProcessSnapshot | None,
                 open_files: OpenFileSnapshot | None,
                 now: float | None = None, checkpoint: Callable[[], None] = lambda: None):
        self.home = normalize_path(home)
        self.root = self.home / ".codex"
        self.protection = protection
        self.process_snapshot = process_snapshot
        self.open_files = open_files
        self.now = time.time() if now is None else now
        self.checkpoint = checkpoint
        self._configs: list[dict] | None = None
        self._config_error = ""
        self._installed: dict[str, tuple[_Tree, dict[str, str]]] = {}
        self.last_complete = True
        self.last_reason_code = ""

    def _check(self, facts: FileFacts, device: int | None = None) -> None:
        value = facts.stat
        if value is None:
            raise ArtifactBlocked("路径状态无法确认")
        if ((isinstance(self.protection, ProtectionGate)
             and self.protection.knowledge_base_ignores(facts.path))
                or self.protection.should_ignore(facts)):
            raise ArtifactBlocked("包含忽略或保护路径", code="protected_path")
        if not (stat.S_ISDIR(value.st_mode) or stat.S_ISREG(value.st_mode)):
            raise ArtifactBlocked("包含符号链接或不支持的文件类型", code="unsupported_file_type")
        if value.st_uid != os.getuid():
            raise ArtifactBlocked("包含不属于当前用户的对象", code="different_owner")
        if device is not None and value.st_dev != device:
            raise ArtifactBlocked("包含跨文件系统对象", code="cross_filesystem")
        if facts.is_probable_cloud_placeholder:
            raise ArtifactBlocked("包含云占位文件", code="cloud_placeholder")
        # 多链接文件不能按整目录释放计量，也可能被目录外继续使用。
        if stat.S_ISREG(value.st_mode) and value.st_nlink != 1:
            raise ArtifactBlocked("包含硬链接文件", code="hardlink_present")

    def _directory(self, path: Path):
        return open_directory(path, self.protection)

    def tree(self, path: Path, *, limit: int = _MAX_ENTRIES) -> _Tree:
        tree = _Tree(path)
        with self._directory(self.root) as root_fd:
            root_stat = os.fstat(root_fd)
            self._check(FileFacts(self.root, root_stat))
            device = root_stat.st_dev

        def visit(directory: Path, fd: int, depth: int):
            self.checkpoint()
            if depth > 32:
                raise ArtifactIncomplete("目录深度超过检查上限", code="inspection_limit")
            before = os.fstat(fd)
            self._check(FileFacts(directory, before), device)
            key = directory.relative_to(path).as_posix()
            tree.entries[key] = before
            with os.scandir(fd) as entries:
                for entry in entries:
                    self.checkpoint()
                    if len(tree.entries) >= limit:
                        raise ArtifactIncomplete("目录条目超过检查上限", code="inspection_limit")
                    child = directory / entry.name
                    # 路径保护先于 lstat 和正文读取。
                    if (isinstance(self.protection, ProtectionGate)
                            and self.protection.knowledge_base_ignores(child)):
                        raise ArtifactBlocked("包含忽略或保护路径", code="protected_path")
                    value = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
                    facts = FileFacts(child, value)
                    self._check(facts, device)
                    if stat.S_ISDIR(value.st_mode):
                        child_fd = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                           dir_fd=fd)
                        try:
                            if _stamp(os.fstat(child_fd)) != _stamp(value):
                                raise ArtifactBlocked("目录在检查期间变化", code="path_changed")
                            visit(child, child_fd, depth + 1)
                        finally:
                            os.close(child_fd)
                    else:
                        tree.entries[child.relative_to(path).as_posix()] = value
                        tree.logical += facts.logical_size
                        tree.allocated += facts.allocated_size
            if _stamp(os.fstat(fd)) != _stamp(before):
                raise ArtifactBlocked("目录在检查期间变化", code="path_changed")

        with self._directory(path) as fd:
            visit(path, fd, 0)
        self.verify(tree)
        tree.newest_mtime = max(s.st_mtime for s in tree.entries.values())
        tree.newest_change = max(max(s.st_mtime, s.st_ctime) for s in tree.entries.values())
        return tree

    def verify(self, tree: _Tree) -> None:
        # 所有文件和目录都复核，不能只看根目录 mtime。
        for name, before in tree.entries.items():
            self.checkpoint()
            path = tree.root / name
            with self._directory(path.parent) as fd:
                after = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
            if _stamp(after) != _stamp(before):
                raise ArtifactBlocked("对象在检查期间变化", code="path_changed")

    def read(self, path: Path, limit: int = _MAX_JSON_BYTES,
             expected: os.stat_result | None = None) -> bytes:
        with self._directory(path.parent) as parent:
            value = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            facts = FileFacts(path, value)
            self._check(facts)
            if not stat.S_ISREG(value.st_mode) or value.st_size > limit:
                raise ArtifactBlocked("证据文件类型或大小不受支持")
            if expected is not None and _stamp(value) != _stamp(expected):
                raise ArtifactBlocked("证据文件在检查期间变化")
            with open_resource(facts, parent) as fd:
                with os.fdopen(os.dup(fd), "rb") as stream:
                    contents = stream.read(limit + 1)
                if len(contents) > limit or _stamp(os.fstat(fd)) != _stamp(value):
                    raise ArtifactBlocked("证据文件在读取期间变化或超过上限")
        return contents

    def configs(self) -> list[dict]:
        if self._config_error:
            raise ArtifactIncomplete(self._config_error, code="config_unavailable")
        if self._configs is None:
            try:
                names = self.names(self.root, _MAX_ROOT_ENTRIES)
                selected = [name for name in names if name in {
                    "config.toml", "managed_config.toml", "requirements.toml",
                } or name.endswith(".config.toml")]
                if len(selected) > 32:
                    raise ArtifactBlocked("配置数量超过检查上限")
                self._configs = [tomllib.loads(self.read(self.root / name).decode("utf-8"))
                                 for name in selected]
                if any(not isinstance(c.get("marketplaces", {}), dict) for c in self._configs):
                    raise ArtifactIncomplete("marketplaces 配置格式不受支持")
            except (OSError, ValueError, UnicodeError, RecursionError):
                self._config_error = "配置引用检查不完整"
                raise ArtifactIncomplete(self._config_error, code="config_unavailable") from None
        return self._configs

    def _check_references(self, path: Path) -> None:
        def values(value):
            if isinstance(value, dict):
                for child in value.values():
                    yield from values(child)
            elif isinstance(value, list):
                for child in value:
                    yield from values(child)
            elif isinstance(value, str):
                yield value

        for config in self.configs():
            for value in values(config):
                if value.startswith("file://"):
                    value = unquote(urlsplit(value).path)
                if not value or "://" in value or "\x00" in value:
                    continue
                # profile 相对路径按 Codex HOME 解析；~ 使用传入的 HOME。
                expanded = self.home / value[2:] if value.startswith("~/") else Path(value)
                reference = normalize_path(expanded if expanded.is_absolute() else self.root / expanded)
                left, right = str(reference).casefold(), str(path).casefold()
                temporary = str(self.root / ".tmp").casefold()
                # 正常 cwd="." / CODEX_HOME 指向整个 HOME，不是对每个临时对象的使用引用。
                if left != temporary and not left.startswith(temporary + "/"):
                    continue
                if left == right or left.startswith(right + "/") or right.startswith(left + "/"):
                    raise ArtifactBlocked("配置引用了该临时目录或其父/子路径", code="config_reference")

    def names(self, path: Path, limit: int) -> list[str]:
        with self._directory(path) as fd:
            before = os.fstat(fd)
            self._check(FileFacts(path, before))
            with os.scandir(fd) as entries:
                names = []
                for entry in entries:
                    self.checkpoint()
                    if len(names) >= limit:
                        raise ArtifactIncomplete("目录枚举超过检查上限", code="inspection_limit")
                    names.append(entry.name)
            if _stamp(os.fstat(fd)) != _stamp(before):
                raise ArtifactBlocked("目录在枚举期间变化")
        return sorted(names)

    def _json(self, tree: _Tree, name: str) -> dict:
        value = json.loads(self.read(tree.root / name, expected=tree.entries[name]))
        if not isinstance(value, dict):
            raise ArtifactBlocked("marketplace 证据格式不受支持")
        return value

    def _manifest(self, tree: _Tree) -> tuple[str, dict]:
        for name in _MANIFESTS:
            if name in tree.entries:
                manifest = self._json(tree, name)
                market = manifest.get("name")
                if (not isinstance(market, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", market)
                        or not isinstance(manifest.get("plugins"), list)):
                    raise ArtifactBlocked("marketplace manifest 不完整或名称不受支持", code="marketplace_manifest_invalid")
                return market, manifest
        raise ArtifactBlocked("缺少支持的 marketplace manifest", code="marketplace_manifest_missing")

    def _digests(self, tree: _Tree) -> dict[str, str]:
        # 正式安装必须已有相同内容；额外文件、修改内容及未知恢复材料不纳入。
        if tree.logical > _MAX_COMPARE_BYTES:
            raise ArtifactIncomplete("marketplace 内容比较超过读取上限", code="inspection_limit")
        digests = {}
        for name, value in tree.entries.items():
            self.checkpoint()
            if Path(name).name == ".DS_Store":
                if not stat.S_ISREG(value.st_mode):
                    raise ArtifactBlocked("Finder 元数据名称对应的文件类型不受支持")
                continue
            digests[name] = ("directory" if stat.S_ISDIR(value.st_mode) else
                             hashlib.sha256(self.read(tree.root / name, _MAX_COMPARE_BYTES,
                                                      expected=value)).hexdigest())
        self.verify(tree)
        return digests

    def _marketplace(self, tree: _Tree) -> str:
        if set(tree.entries) == {"."}:
            return "空升级暂存目录"
        market, manifest = self._manifest(tree)
        if _METADATA not in tree.entries:
            raise ArtifactIncomplete("暂存缺少安装元数据，无法确认来源和 revision", code="marketplace_metadata_missing")
        metadata = self._json(tree, _METADATA)
        if (set(metadata) != {"source_type", "source", "ref_name", "sparse_paths", "revision"}
                or metadata.get("source_type") != "git"
                or not isinstance(metadata.get("source"), str) or not metadata["source"]
                or not isinstance(metadata.get("revision"), str) or not metadata["revision"]
                or not isinstance(metadata.get("sparse_paths"), list)
                or not all(isinstance(p, str) for p in metadata["sparse_paths"])
                or not (metadata["ref_name"] is None or isinstance(metadata["ref_name"], str))):
            raise ArtifactBlocked("Git marketplace 安装元数据不完整", code="marketplace_metadata_invalid")
        configs = [c.get("marketplaces", {}).get(market) for c in self.configs()
                   if isinstance(c.get("marketplaces", {}), dict) and market in c.get("marketplaces", {})]
        if not configs:
            raise ArtifactBlocked("当前配置未登记该 marketplace", code="marketplace_not_configured")
        if any(not isinstance(c, dict) or (
            c.get("source_type"), c.get("source"), c.get("ref"), c.get("sparse_paths") or [],
        ) != ("git", metadata["source"], metadata["ref_name"], metadata["sparse_paths"])
            for c in configs):
            raise ArtifactBlocked("marketplace 来源与当前配置不一致", code="marketplace_source_mismatch")
        installed = self.root / ".tmp/marketplaces" / market
        if market not in self._installed:
            try:
                installed_tree = self.tree(installed)
            except FileNotFoundError:
                raise ArtifactIncomplete("正式安装目录或其文件缺失", code="marketplace_install_missing") from None
            self._installed[market] = (installed_tree, self._digests(installed_tree))
        installed_tree, installed_digests = self._installed[market]
        self.verify(installed_tree)
        if _METADATA not in installed_tree.entries:
            raise ArtifactIncomplete("正式安装缺少安装元数据", code="marketplace_install_metadata_missing")
        installed_metadata = self._json(installed_tree, _METADATA)
        if installed_metadata.get("revision") != metadata["revision"]:
            raise ArtifactBlocked("暂存 revision 与正式安装不同", code="marketplace_revision_mismatch")
        if installed_metadata != metadata:
            raise ArtifactBlocked("暂存来源与正式安装元数据不一致", code="marketplace_install_source_mismatch")
        if self._manifest(installed_tree) != (market, manifest):
            raise ArtifactBlocked("暂存 manifest 与正式安装不同", code="marketplace_manifest_mismatch")
        if self._digests(tree) != installed_digests:
            raise ArtifactBlocked("暂存包含与正式安装不同的文件或目录", code="marketplace_content_differs")
        return "正式安装已有相同来源、revision 和内容"

    def assess(self, path: Path, kind: str) -> tuple[_Tree | None, str, str]:
        tree = None
        self.last_complete = True
        self.last_reason_code = ""
        try:
            tree = self.tree(path, limit=10 if kind == "git" else _MAX_ENTRIES)
            self._check_references(path)
            if kind == "git":
                required = {".", "HEAD", "objects", "refs"}
                allowed = required | {".DS_Store", "objects/.DS_Store", "refs/.DS_Store"}
                missing = required - tree.entries.keys()
                if missing:
                    raise ArtifactBlocked("Git 骨架缺少 " + "/".join(sorted(missing)), code="git_entries_missing")
                if not all(stat.S_ISDIR(tree.entries[n].st_mode) for n in ("objects", "refs")):
                    raise ArtifactBlocked("Git objects/refs 不是目录", code="git_directory_type_invalid")
                if any(n.startswith(("objects/", "refs/")) and n not in allowed for n in tree.entries):
                    raise ArtifactBlocked("Git objects/refs 中存在实际数据", code="git_data_present")
                if not tree.entries.keys() <= allowed:
                    raise ArtifactBlocked("Git 临时目录包含骨架以外的文件或目录", code="git_extra_entries")
                if any(not stat.S_ISREG(s.st_mode) for n, s in tree.entries.items()
                       if n not in {".", "objects", "refs"}):
                    raise ArtifactBlocked("Git HEAD 或 Finder 元数据不是普通文件", code="git_file_type_invalid")
                if tree.entries["HEAD"].st_size == 0:
                    raise ArtifactBlocked("Git HEAD 为空，骨架尚不完整", code="git_head_empty")
                if (tree.entries["HEAD"].st_size > 64 or self.read(path / "HEAD", 64, tree.entries["HEAD"])
                        not in {b"ref: refs/heads/main", b"ref: refs/heads/main\n"}):
                    raise ArtifactBlocked("Git HEAD 内容不是预期的默认分支", code="git_head_unexpected")
                note = "HEAD 为默认分支，objects/refs 无 Git 数据"
            else:
                note = self._marketplace(tree)
            if self.now - tree.newest_change < MINIMUM_AGE_DAYS[kind] * 86400:
                raise ArtifactBlocked(f"mtime/ctime 未静置满 {MINIMUM_AGE_DAYS[kind]} 天", code="minimum_age_not_met")
            if self.process_snapshot is None:
                raise ArtifactIncomplete("Codex 进程状态未知", code="process_status_unknown")
            if self.process_snapshot.any_running(PROCESS_MARKERS):
                raise ArtifactBlocked("Codex 正在运行", code="codex_running")
            if self.open_files is None:
                raise ArtifactIncomplete("打开句柄状态未知", code="open_files_unknown")
            if self.open_files.count_under(path):
                raise ArtifactBlocked("存在打开句柄", code="open_handles_present")
            self.verify(tree)
            return tree, "", note
        except ArtifactIncomplete as exc:
            self.last_complete = False
            self.last_reason_code = exc.code
            return tree, str(exc), ""
        except ArtifactBlocked as exc:
            self.last_reason_code = exc.code
            return tree, str(exc), ""
        except (OSError, ValueError, KeyError, UnicodeError, RecursionError):
            self.last_complete = False
            self.last_reason_code = "evidence_unavailable"
            return tree, "结构、配置或正式安装证据检查不完整", ""


def scan_codex_transient_targets(
    protection: Predicate, *, home: Path, process_snapshot: ProcessSnapshot | None,
    open_files: OpenFileSnapshot | None, now: float | None = None,
    checkpoint: Callable[[], None] = lambda: None,
    on_progress: Callable[[], None] = lambda: None,
) -> ScanResult:
    result = ScanResult()
    context = CodexArtifactContext(home, protection, process_snapshot=process_snapshot,
                                   open_files=open_files, now=now, checkpoint=checkpoint)
    from .engine import Cancelled

    checked = 0
    try:
        for kind, relative, prefix in (
            ("git", ".tmp", "git-"),
            ("marketplace", ".tmp/marketplaces/.staging", "marketplace-upgrade-"),
        ):
            root = context.root / relative
            try:
                with context._directory(context.root) as fd:
                    root_identity = FileIdentity.from_stat(os.fstat(fd))
                names = [name for name in context.names(root, _MAX_ROOT_ENTRIES) if name.startswith(prefix)]
            except FileNotFoundError:
                continue
            except OSError:
                result.issues.append(ScanIssue("codex_enumeration_incomplete",
                    "Codex 临时目录枚举不完整或受保护", _TASKS[kind], root))
                continue
            truncated = len(names) > MAX_TARGETS[kind]
            if truncated:
                result.issues.append(ScanIssue("diagnostic_limit_reached",
                    "目标超过检查上限；保留已检查子集，其余未扫描", _TASKS[kind], root))
            start = len(result.items)
            blocked: Counter[tuple[str, str]] = Counter()
            for name in names[:MAX_TARGETS[kind]]:
                checkpoint()
                path = root / name
                tree, reason, note = context.assess(path, kind)
                checked += 1
                on_progress()
                if tree is None:
                    blocked[(context.last_reason_code, reason)] += 1
                    result.issues.append(ScanIssue("codex_target_unverified", reason, _TASKS[kind], path))
                    continue
                if not context.last_complete:
                    result.issues.append(ScanIssue("codex_evidence_incomplete", reason, _TASKS[kind], path))
                age = max(0, int((context.now - tree.newest_change) // 86400))
                item = Item(
                    path, tree.allocated, _TASKS[kind], "critical",
                    f"{note or reason}；静置 {age} 天（按最新 mtime/ctime）",
                    logical_size=tree.logical, allocated_size=tree.allocated,
                    actionable=not reason, action_block_reason=reason, preselected=False,
                    identity=FileIdentity.from_stat(tree.entries["."]), domain="ai",
                    requires_explicit_selection=True, running_process_markers=PROCESS_MARKERS,
                    cleanup_scope="codex-transient", cleanup_root=context.root,
                    cleanup_root_identity=root_identity, latest_mtime=tree.newest_mtime,
                    age_days=age,
                    open_handle_count=open_files.count_under(path) if open_files is not None else None,
                )
                # 有界前缀可预览；扫描不完整时不解锁该组动作。
                if truncated:
                    item = replace(item, actionable=False, action_block_reason="扫描覆盖不完整")
                    blocked[("scan_incomplete", item.action_block_reason)] += 1
                elif reason:
                    blocked[(context.last_reason_code, reason)] += 1
                result.items.append(item)
            result.diagnostics.append({
                "kind": "codex_transient_targets", "category": _TASKS[kind], "anchor_path": str(root),
                "total_count": len(names), "checked_count": min(len(names), MAX_TARGETS[kind]),
                "actionable_count": sum(item.actionable for item in result.items[start:]),
                "complete": not truncated and not any(i.path and i.path.is_relative_to(root) for i in result.issues),
                "minimum_age_days": MINIMUM_AGE_DAYS[kind], "age_basis": "newest_mtime_or_ctime",
                "block_reasons": [
                    {"code": code, "message": message, "count": count}
                    for (code, message), count in sorted(blocked.items())
                ],
            })
    except Cancelled:
        result.cancelled = True
        result.items = [replace(item, actionable=False, action_block_reason="扫描已取消")
                        for item in result.items]
        result.diagnostics = [row for row in result.diagnostics if row.get("kind") != "codex_transient_targets"]
        result.diagnostics.append({
            "kind": "codex_transient_targets", "category": "Codex 临时对象",
            "anchor_path": str(context.root), "checked_count": checked, "actionable_count": 0, "complete": False,
            "block_reasons": [{"code": "scan_cancelled", "message": "扫描已取消", "count": checked}],
        })
    return result
