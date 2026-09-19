"""按文件元数据递归发现大文件；没有清理或文件内容读取路径。"""
from __future__ import annotations

import heapq
import os
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path

from .engine import Cancelled, Control, IgnoreRules, _append_issue, _inspect_path
from .filesystem import filesystem_id_retry, lstat_retry, scandir_entries
from .macos import scan_symlink_anchor
from .models import FileFacts, Item, ScanIssue, normalize_path
from .predicates import Predicate, ProtectionGate

DEFAULT_MIN_SIZE = 100 * 1024 * 1024
DEFAULT_MAX_ENTRIES = 200_000


class LargeFilesError(ValueError):
    pass


@dataclass
class LargeFileScan:
    root: Path
    items: list[Item] = field(default_factory=list)
    issues: list[ScanIssue] = field(default_factory=list)
    cancelled: bool = False
    scanned_entries: int = 0
    scanned_files: int = 0
    matched_files: int = 0
    logical_bytes: int = 0
    allocated_bytes: int = 0
    skipped: dict[str, int] = field(default_factory=lambda: dict.fromkeys(
        ("ignored", "unreadable", "symlinks", "cloud_placeholders", "cross_filesystem", "hardlink_aliases", "special_files"), 0,
    ))

    @property
    def complete(self) -> bool:
        return not self.cancelled and not any(issue.blocking for issue in self.issues)

    @property
    def truncated(self) -> bool:
        return len(self.items) < self.matched_files


def _validated_root(root: Path, protection: Predicate) -> FileFacts:
    # 在读取后代元数据前逐级检查；尤其不能越过 dataless 祖先去枚举用户指定子目录。
    current = scan_symlink_anchor(root)
    for part in ("", *root.relative_to(current).parts):
        if part:
            current /= part
        if isinstance(protection, ProtectionGate) and protection.knowledge_base_ignores(current):
            raise LargeFilesError(f"扫描路径命中忽略或保护规则：{current}")
        try:
            facts = FileFacts(current, lstat_retry(current))
        except OSError as exc:
            raise LargeFilesError(f"无法访问扫描路径 {current}：{exc}") from exc
        if stat.S_ISLNK(facts.stat.st_mode):
            raise LargeFilesError(f"扫描路径包含符号链接：{current}")
        if facts.is_probable_cloud_placeholder:
            raise LargeFilesError(f"扫描路径包含 dataless/疑似云占位：{current}")
        if protection.should_ignore(facts):
            raise LargeFilesError(f"扫描路径命中忽略或保护规则：{current}")
        if not stat.S_ISDIR(facts.stat.st_mode):
            raise LargeFilesError(f"扫描路径不是目录：{current}")
    return facts


def scan_large_files(
    path: str | os.PathLike[str],
    *,
    min_size: int = DEFAULT_MIN_SIZE,
    top: int = 50,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    protection: Predicate | None = None,
    control: Control | None = None,
) -> LargeFileScan:
    """按 logical size 筛选普通文件，独立计量 allocated size，结果始终不可执行。"""
    if min_size < 1 or top < 0 or max_entries < 1:
        raise ValueError("min_size/max_entries 必须为正，top 必须非负")
    root = normalize_path(path)
    protection = protection or IgnoreRules()
    control = control or Control()
    root_facts = _validated_root(root, protection)
    result = LargeFileScan(root)
    try:
        filesystem = filesystem_id_retry(root)
    except OSError as exc:
        result.skipped["unreadable"] += 1
        _append_issue(result.issues, exc, root, "large")
        return result
    directories = [root_facts]
    seen: set[tuple[int, int]] = set()
    retained: list[tuple[int, str, Item]] = []
    observed_at = time.time()
    exhausted = False
    try:
        while directories and not exhausted:
            control.checkpoint()
            previous = directories.pop()
            issue_count = len(result.issues)
            directory = _inspect_path(previous.path, result.issues, "large", protection, missing_is_issue=True)
            if directory is None:
                result.skipped["unreadable" if len(result.issues) > issue_count else "ignored"] += 1
                continue
            if stat.S_ISLNK(directory.stat.st_mode):
                result.skipped["symlinks"] += 1
                continue
            if directory.identity != previous.identity or not stat.S_ISDIR(directory.stat.st_mode):
                result.issues.append(ScanIssue(
                    code="path_changed", message="目录在扫描期间发生替换，已跳过",
                    task="large", path=directory.path,
                ))
                continue
            if directory.is_probable_cloud_placeholder:
                result.skipped["cloud_placeholders"] += 1
                continue
            try:
                if (directory.stat.st_dev != root_facts.stat.st_dev
                        or filesystem_id_retry(directory.path) != filesystem):
                    result.skipped["cross_filesystem"] += 1
                    continue
                for entry in scandir_entries(directory.path):
                    control.checkpoint()
                    if result.scanned_entries >= max_entries:
                        exhausted = True
                        result.issues.append(ScanIssue(
                            code="scan_limit_reached", message=f"达到 {max_entries} 个目录项上限，扫描不完整",
                            task="large", path=root,
                        ))
                        break
                    result.scanned_entries += 1
                    issue_count = len(result.issues)
                    facts = _inspect_path(entry.path, result.issues, "large", protection, missing_is_issue=True)
                    if facts is None:
                        result.skipped["unreadable" if len(result.issues) > issue_count else "ignored"] += 1
                        continue
                    mode = facts.stat.st_mode
                    if stat.S_ISLNK(mode):
                        result.skipped["symlinks"] += 1
                        continue
                    if facts.is_probable_cloud_placeholder:
                        result.skipped["cloud_placeholders"] += 1
                        continue
                    if facts.stat.st_dev != root_facts.stat.st_dev:
                        result.skipped["cross_filesystem"] += 1
                        continue
                    if stat.S_ISDIR(mode):
                        directories.append(facts)
                        continue
                    if not stat.S_ISREG(mode):
                        result.skipped["special_files"] += 1
                        continue
                    result.scanned_files += 1
                    if facts.logical_size < min_size:
                        continue
                    key = (facts.stat.st_dev, facts.stat.st_ino)
                    if key in seen:
                        result.skipped["hardlink_aliases"] += 1
                        continue
                    seen.add(key)
                    result.matched_files += 1
                    result.logical_bytes += facts.logical_size
                    result.allocated_bytes += facts.allocated_size
                    item = Item(
                        facts.path, facts.allocated_size, "大文件", "critical",
                        "文件大小和修改时间仅供审阅，不表示垃圾或可释放空间；本命令只读",
                        logical_size=facts.logical_size, allocated_size=facts.allocated_size,
                        actionable=False, action_block_reason="大文件扫描仅只读",
                        identity=facts.identity, latest_mtime=facts.stat.st_mtime,
                        age_days=max(0, int((observed_at - facts.stat.st_mtime) // 86400)),
                        preselected=False, domain="large",
                    )
                    ranked = (facts.logical_size, str(facts.path), item)
                    if top == 0 or len(retained) < top:
                        heapq.heappush(retained, ranked)
                    elif ranked[:2] > retained[0][:2]:
                        heapq.heapreplace(retained, ranked)
            except OSError as exc:
                result.skipped["unreadable"] += 1
                _append_issue(result.issues, exc, directory.path, "large")
    except (Cancelled, KeyboardInterrupt):
        control.cancel()
        result.cancelled = True
    result.items = [entry[2] for entry in sorted(retained, reverse=True)]
    return result
