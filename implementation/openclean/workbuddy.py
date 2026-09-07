"""WorkBuddy 已观察目录结构的只读识别；个人经验来源见 docs/EXPERIENCE.md。

不把 expired 名称、Worker 年龄或 Electron 目录名转换成删除授权。
"""
from __future__ import annotations

import re
import stat
from dataclasses import replace
from datetime import date
from pathlib import Path

from .filesystem import lstat_retry, scandir_entries
from .macos import scan_symlink_anchor, symlink_component
from .models import FileFacts, ScanIssue, ScanResult, normalize_path
from .predicates import Predicate
from .processes import OpenFileSnapshot, ProcessSnapshot
from .storage_diagnostics import (
    RetentionRule, _append_filesystem_issue, _capture_runtime_snapshots,
    _knowledge_base_ignores, _predicates_ignore_after_knowledge_base,
    scan_retention_rules,
)

PROCESS_MARKERS = ("WorkBuddy.app", "com.workbuddy.workbuddy", "com.tencent.workbuddy")
CACHE_NAMES = ("Cache", "Code Cache", "GPUCache", "DawnGraphiteCache", "DawnWebGPUCache")
_EXPIRED = re.compile(r"(\d{4}-\d{2}-\d{2})\.expired-\d{13}-[0-9a-f]{8}", re.ASCII)


def scan_workbuddy_storage(
    protection: Predicate, *, home: Path | None = None,
    snapshots: tuple[ProcessSnapshot | None, OpenFileSnapshot | None] | None = None,
    now: float | None = None, max_entries: int = 5000,
) -> ScanResult:
    result = ScanResult()
    base = normalize_path(home or Path.home()) / ".workbuddy"
    task = "WorkBuddy 经验结构"
    remaining = max_entries

    def directory(path: Path, device: int | None = None) -> FileFacts | None:
        if _knowledge_base_ignores(protection, path):
            return None
        if symlink_component(path, anchor=scan_symlink_anchor(path)) is not None:
            result.issues.append(ScanIssue(code="unsafe_symlink_ancestor",
                message="WorkBuddy 诊断拒绝符号链接", task=task, path=path, blocking=False))
            return None
        try:
            facts = FileFacts(path, lstat_retry(path))
        except FileNotFoundError:
            return None
        except OSError as exc:
            _append_filesystem_issue(result.issues, exc, path, task)
            return None
        if (not stat.S_ISDIR(facts.stat.st_mode) or facts.is_probable_cloud_placeholder
                or _predicates_ignore_after_knowledge_base(protection, facts)):
            return None
        if device is not None and facts.stat.st_dev != device:
            result.issues.append(ScanIssue(code="cross_device_path",
                message="WorkBuddy 诊断跳过跨卷目录", task=task, path=path, blocking=True))
            return None
        return facts

    base_facts = directory(base)
    if base_facts is None:
        return result
    device = base_facts.stat.st_dev

    def children(path: Path):
        nonlocal remaining
        if directory(path, device) is None:
            return
        try:
            for entry in scandir_entries(path):
                if remaining <= 0:
                    result.issues.append(ScanIssue(code="diagnostic_entry_limit",
                        message="WorkBuddy 结构发现超过条目上限", task=task, path=path, blocking=True))
                    return
                remaining -= 1
                yield Path(entry.path)
        except OSError as exc:
            _append_filesystem_issue(result.issues, exc, path, task)

    targets: list[tuple[Path, str, str, bool]] = []
    for path in children(base / "logs"):
        match = _EXPIRED.fullmatch(path.name)
        if match is None:
            continue
        try:
            date.fromisoformat(match[1])
        except ValueError:
            continue
        targets.append((path, "WorkBuddy expired 日志", "匹配日期目录的 .expired- 后缀；名称不代表可删除", False))
    for path in children(base / "traces"):
        if re.fullmatch(r"[0-9]+", path.name, re.ASCII):
            targets.append((path, "WorkBuddy Worker 整组 traces",
                "age_days 为整组文件及目录的最新修改时间；不能按单文件 mtime 拆删 Worker", True))
    session = base / "app/session"
    session_roots = [session]
    session_roots.extend(children(session / "Partitions"))
    for root in session_roots:
        if directory(root, device) is not None:
            for name in CACHE_NAMES:
                targets.append((root / name, "WorkBuddy Electron 精确缓存",
                    "仅识别精确缓存子目录；不包含 Cookies、IndexedDB、Local Storage 或整个 session", False))
    processes, open_files = snapshots if snapshots is not None else _capture_runtime_snapshots(result, task=task)
    for path, category, explanation, group_age in targets:
        if directory(path, device) is None:
            continue
        scanned = scan_retention_rules(
            (RetentionRule(category, str(path), PROCESS_MARKERS),), protection,
            process_snapshot=processes, open_files=open_files, now=now,
            include_directory_mtime=group_age, entry_limit=100_000,
        )
        result.issues.extend(scanned.issues)
        for item in scanned.items:
            incomplete_group = group_age and (
                item.excluded_paths or item.cross_device_paths or item.cloud_file_count or scanned.issues
            )
            note = f"{explanation}；{item.note}"
            if incomplete_group:
                note += "；整组存在跳过或测量错误，整组年龄未知"
            result.items.append(replace(item, note=note,
                age_days=None if incomplete_group else item.age_days,
                latest_mtime=None if incomplete_group else item.latest_mtime,
                action_block_reason="WorkBuddy 已观察结构仅诊断，尚无清理动作验证"))
    return result
