"""只读磁盘空间分析核心。"""
from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass, field, replace
from pathlib import Path

from .engine import Control, IgnoreRules, scan_points
from .macos import (
    discover_local_snapshots,
    nonprivileged_action_block_reason,
    scan_symlink_anchor,
    symlink_component,
    volume_mount_point,
)
from .models import FileFacts, Item, ScanIssue, normalize_path
from .predicates import Predicate
from .scanpoints import ScanPoint


class AnalyzeError(ValueError):
    pass


@dataclass(frozen=True)
class SpaceEntry:
    item: Item
    percent: float


@dataclass
class SpaceAnalysis:
    root: Path
    entries: list[SpaceEntry] = field(default_factory=list)
    issues: list[ScanIssue] = field(default_factory=list)
    cancelled: bool = False
    volume_total: int | None = None
    volume_used: int | None = None
    volume_free: int | None = None
    snapshot_mount_point: Path | None = None
    local_snapshots: tuple[str, ...] = ()
    local_snapshots_checked: bool = False
    local_snapshot_size: int | None = None

    @property
    def total(self) -> int:
        return sum(entry.item.size for entry in self.entries)

    @property
    def complete(self) -> bool:
        return not self.cancelled and not any(
            issue.blocking for issue in self.issues
        )


def analyze_path(
    path: str | os.PathLike[str],
    *,
    protection: Predicate | None = None,
    control: Control | None = None,
) -> SpaceAnalysis:
    """统计指定目录的一级子项，子目录大小递归计算并按大小排序。"""
    root = normalize_path(path)
    protection = protection or IgnoreRules()
    control = control or Control()
    try:
        root_stat = root.lstat()
    except FileNotFoundError as exc:
        raise AnalyzeError(f"分析路径不存在：{root}") from exc
    except (PermissionError, OSError) as exc:
        raise AnalyzeError(f"无法访问分析路径 {root}：{exc}") from exc
    if stat.S_ISLNK(root_stat.st_mode):
        raise AnalyzeError(f"分析根路径不能是符号链接：{root}")
    if component := symlink_component(
        root,
        anchor=scan_symlink_anchor(root),
    ):
        raise AnalyzeError(f"分析根路径包含符号链接组件：{component}")
    if not stat.S_ISDIR(root_stat.st_mode):
        raise AnalyzeError(f"分析路径不是目录：{root}")
    root_facts = FileFacts(path=root, stat=root_stat)
    if protection.should_ignore(root_facts):
        raise AnalyzeError(f"分析路径命中忽略或保护规则：{root}")

    analysis = SpaceAnalysis(root=root)
    try:
        with os.scandir(root) as iterator:
            candidate_paths = tuple(entry.path for entry in iterator)
    except (PermissionError, FileNotFoundError, OSError) as exc:
        analysis.issues.append(
            ScanIssue(
                code=(
                    "permission_denied"
                    if isinstance(exc, PermissionError)
                    else "filesystem_error"
                ),
                message=str(exc),
                task="analyze",
                path=root,
            )
        )
        return analysis

    scan_result = scan_points(
        [
            ScanPoint(
                "空间占用",
                candidate_paths,
                "confirm",
                "只读空间分析",
                domain="analyze",
            )
        ],
        ctl=control,
        ignore=protection,
        workers=1,
    )
    analysis.issues.extend(scan_result.issues)
    analysis.cancelled = scan_result.cancelled
    scan_result.items = [
        replace(
            item,
            actionable=False,
            action_block_reason=reason,
            preselected=False,
            safety="critical",
        )
        if (reason := nonprivileged_action_block_reason(item.path))
        else item
        for item in scan_result.items
    ]
    total = scan_result.total
    analysis.entries = [
        SpaceEntry(
            item=item,
            percent=(item.size / total * 100.0 if total else 0.0),
        )
        for item in sorted(scan_result.items, key=lambda candidate: -candidate.size)
    ]

    try:
        usage = shutil.disk_usage(root)
    except OSError as exc:
        analysis.issues.append(
            ScanIssue(
                code="filesystem_error",
                message=str(exc),
                task="analyze-volume",
                path=root,
            )
        )
    else:
        analysis.volume_total = usage.total
        analysis.volume_used = usage.used
        analysis.volume_free = usage.free

    try:
        mount_point = volume_mount_point(root)
    except (PermissionError, FileNotFoundError, OSError) as exc:
        analysis.issues.append(
            ScanIssue(
                code="snapshot_discovery_failed",
                message=f"无法定位卷挂载点：{exc}",
                task="time-machine-local-snapshots",
                path=root,
                blocking=False,
            )
        )
    else:
        analysis.snapshot_mount_point = mount_point
        if root == mount_point:
            snapshots = discover_local_snapshots(mount_point)
            analysis.local_snapshots_checked = not snapshots.issues
            analysis.local_snapshots = snapshots.snapshots
            analysis.issues.extend(snapshots.issues)
    return analysis
