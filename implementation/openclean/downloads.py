"""指定目录中的下载文件和镜像候选，只读归类；不把用户文件视为垃圾。"""

from __future__ import annotations

import os
import stat
import time
from collections.abc import Callable, Iterable

from .disk_images import (
    IMAGE_SUFFIXES,
    attached_images,
    image_format_description,
    image_mount_state,
)
from .models import FileFacts, Item, ScanIssue, ScanResult, normalize_path
from .predicates import Predicate, ProtectionGate
from .resource_walk import WalkBudget, open_resource, walk_resources

_INSTALLERS = frozenset({".pkg", ".mpkg"})
_ARCHIVES = frozenset({".zip", ".gz", ".bz2", ".xz", ".7z", ".rar", ".tar"})
_IN_PROGRESS = frozenset({".crdownload", ".download", ".part"})


def scan_downloads(
    roots: Iterable[str | os.PathLike[str]],
    protection: Predicate,
    *,
    checkpoint: Callable[[], None] = lambda: None,
    on_progress: Callable[[], None] = lambda: None,
    max_entries: int = 200_000,
    max_files: int = 4096,
    mount_runner=None,
    format_runner=None,
) -> ScanResult:
    from .engine import Cancelled

    result = ScanResult()
    task = "下载与磁盘镜像"
    budget = WalkBudget(max_entries)
    seen: set[tuple[int, int]] = set()
    images = None
    queried_mounts = False
    format_probes = 0
    now = time.time()
    try:
        for raw_root in roots:
            root = normalize_path(raw_root)
            if isinstance(
                protection, ProtectionGate
            ) and protection.knowledge_base_ignores(root):
                continue
            facts = FileFacts.from_path(root)
            if facts.stat is None or protection.should_ignore(facts):
                continue
            details: list[dict[str, object]] = []
            start = len(result.issues)
            for child, parent_fd in walk_resources(
                root,
                protection,
                result,
                task,
                budget=budget,
                checkpoint=checkpoint,
                on_progress=on_progress,
                descend=lambda f: (
                    f.path.suffix.casefold()
                    not in {".app", ".sparsebundle", ".download"}
                ),
            ):
                if not stat.S_ISREG(child.stat.st_mode):
                    continue
                identity = (child.stat.st_dev, child.stat.st_ino)
                if identity in seen:
                    continue
                seen.add(identity)
                suffix = child.path.suffix.casefold()
                kind = (
                    "disk_image"
                    if suffix in IMAGE_SUFFIXES
                    else "in_progress"
                    if suffix in _IN_PROGRESS
                    else "installer"
                    if suffix in _INSTALLERS
                    else "archive"
                    if suffix in _ARCHIVES
                    else "other"
                )
                entry: dict[str, object] = {
                    "path": str(child.path),
                    "file_type": kind,
                    "logical_bytes": child.logical_size,
                    "allocated_bytes": child.allocated_size,
                    "latest_mtime": child.stat.st_mtime,
                    "age_days": max(0, int((now - child.stat.st_mtime) // 86400)),
                    "time_basis": "mtime_not_last_used_or_download_time",
                    "usage": "unknown",
                    "actionable": False,
                    "context": (
                        "development_files"
                        if {"testdata", "node_modules", ".git"}.intersection(
                            child.path.parts
                        )
                        else "user_files"
                    ),
                }
                if kind == "disk_image":
                    if not queried_mounts:
                        images = attached_images(runner=mount_runner)
                        queried_mounts = True
                        if images is None:
                            result.issues.append(
                                ScanIssue(
                                    "mount_state_unknown",
                                    "挂载信息读取失败，未挂载状态不能确认",
                                    task,
                                    root,
                                    False,
                                )
                            )
                    entry["mount_state"] = image_mount_state(child.path, images)
                    # 将已检查的 fd 交给系统 file，不重新按可替换的祖先路径打开。
                    description = None
                    if format_probes < 64:
                        format_probes += 1
                        try:
                            with open_resource(child, parent_fd) as descriptor:
                                description = image_format_description(
                                    descriptor, runner=format_runner
                                )
                        except OSError as exc:
                            description = None
                            result.issues.append(
                                ScanIssue(
                                    "permission_denied"
                                    if isinstance(exc, PermissionError)
                                    else "path_changed",
                                    str(exc),
                                    task,
                                    child.path,
                                )
                            )
                    entry["format_evidence_status"] = (
                        "reported" if description else "unknown"
                    )
                    entry["format_description"] = description
                    entry["type_basis"] = "extension_candidate_with_format_evidence"
                details.append(entry)
                if len(details) >= max_files:
                    result.issues.append(
                        ScanIssue(
                            "scan_limit_reached", "文件审阅明细达到上限", task, root
                        )
                    )
                    break
            for kind, category in (
                ("disk_image", "磁盘镜像候选"),
                ("downloads", "下载文件审阅"),
            ):
                files = [
                    f
                    for f in details
                    if (f["file_type"] == "disk_image") == (kind == "disk_image")
                ]
                if not files:
                    continue
                allocated = sum(int(f["allocated_bytes"]) for f in files)
                logical = sum(int(f["logical_bytes"]) for f in files)
                note = (
                    "普通文件只读分类；时间为修改时间，不代表最后使用；"
                    "镜像未挂载不代表不使用；容量单列，不与其它扫描分类相加"
                )
                result.items.append(
                    Item(
                        root,
                        0,
                        category,
                        "critical",
                        note=note,
                        resource_kind="filesystem_subset",
                        diagnostic_kind=kind,
                        resource_total_size=allocated,
                        logical_size=logical,
                        total_count=len(files),
                        identity=facts.identity,
                        actionable=False,
                        preselected=False,
                        action_block_reason="用户文件仅审阅，未启用清理动作",
                    )
                )
                result.diagnostics.append(
                    {
                        "kind": kind,
                        "anchor_path": str(root),
                        "category": category,
                        "complete": not any(i.blocking for i in result.issues[start:]),
                        "logical_bytes": logical,
                        "allocated_bytes": allocated,
                        "measurement_basis": "review_files_separate_total",
                        "files": files,
                    }
                )
            if budget.exhausted:
                break
    except Cancelled:
        result.cancelled = True
    return result
