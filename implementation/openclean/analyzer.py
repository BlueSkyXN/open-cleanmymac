"""只读磁盘空间分析核心。"""
from __future__ import annotations

import os
import shutil
import stat
import threading
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from pathlib import Path

from .engine import Cancelled, Control, IgnoreRules, _deduplicate_hardlinks, _scan_guard_context, _scan_point
from .filesystem import lstat_retry, scandir_entries
from .macos import (
    discover_local_snapshots,
    nonprivileged_action_block_reason,
    scan_symlink_anchor,
    symlink_component,
    volume_mount_point,
)
from .models import FileFacts, Item, ScanIssue, ScanResult, normalize_path
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
    # 浏览补充行不进入 CLI/JSON 的清理候选集合。
    browse_entries: list[SpaceEntry] | None = None
    root_identity: tuple[int, int, int, int] | None = None

    @property
    def total(self) -> int:
        return sum(entry.item.size for entry in self.entries)

    @property
    def complete(self) -> bool:
        return not self.cancelled and not any(
            issue.blocking for issue in self.issues
        )


@dataclass(frozen=True)
class AnalysisUpdate:
    completed: int
    total: int
    entries: tuple[Item, ...] = ()
    # 活跃 worker 最近实际处理的路径；不包含排队任务，不额外遍历。
    current_paths: tuple[str, ...] = ()


def _scan_candidates(paths, control, protection, workers, on_update, include_empty):
    """有界批次调度；最终按发现顺序合并，硬链接归属不依赖线程完成顺序。"""
    guards = _scan_guard_context(protection)
    batch_size = min(64, max(1, (len(paths) + workers * 4 - 1) // (workers * 4)))
    completed = 0
    next_index = 0
    results = {}
    preview: list[Item] = []
    emitted = 0.0
    interrupted = False
    active_paths = {}
    path_lock = threading.Lock()

    def scan_batch(index, point):
        def visit(path):
            with path_lock:
                active_paths[index] = str(path)
        try:
            return _scan_point(point, control, protection, guards=guards,
                               include_empty=include_empty,
                               on_path=visit if on_update is not None else None)
        finally:
            with path_lock:
                active_paths.pop(index, None)

    with control.delegating(), ThreadPoolExecutor(max_workers=workers, thread_name_prefix="analyze") as pool:
        pending = {}
        try:
            while pending or next_index < len(paths):
                control.checkpoint()
                while next_index < len(paths) and len(pending) < workers * 2:
                    batch = paths[next_index:next_index + batch_size]
                    point = ScanPoint("空间占用", batch, "critical",
                                      "空间分析只表示实际占用，不代表垃圾或可回收空间",
                                      domain="analyze", stay_on_device=True)
                    future = pool.submit(control.run_task, scan_batch, next_index, point)
                    pending[future] = (next_index, len(batch))
                    next_index += len(batch)
                done, _ = wait(pending, timeout=0.05, return_when=FIRST_COMPLETED)
                for future in done:
                    index, count = pending.pop(future)
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = ScanResult(issues=[ScanIssue(
                            "task_failed", f"{type(exc).__name__}: {exc}", "空间占用")])
                    results[index] = result
                    failed = result.cancelled or any(issue.code == "task_failed" for issue in result.issues)
                    completed += len(result.items) if failed else count
                    # 只向 UI 提供有界示例，完整结果始终单独保留。
                    preview.extend(result.items[:max(0, 128 - len(preview))])
                now = time.monotonic()
                if on_update is not None and (now - emitted >= 0.1 or completed == len(paths)):
                    with path_lock:
                        current = tuple(path for _, path in sorted(active_paths.items()))
                    on_update(
                        AnalysisUpdate(
                            completed, len(paths), tuple(preview),
                            current_paths=current,
                        )
                    )
                    emitted = now
        except Cancelled:
            control.cancel()
            interrupted = True
        except BaseException:
            # 必须在 executor.__exit__ 等待线程前传递取消。
            control.cancel()
            raise
        finally:
            for future, (index, _) in pending.items():
                try:
                    results[index] = future.result()
                except Exception as exc:
                    results[index] = ScanResult(issues=[ScanIssue(
                        "task_failed", f"{type(exc).__name__}: {exc}", "空间占用")])
    combined = ScanResult(cancelled=interrupted)
    for index in sorted(results):
        combined.extend(results[index])
    # 旧单任务只报告一次的全局保护问题，不因批次拆分而重复。
    guard_codes = set()
    issues = []
    for issue in combined.issues:
        if issue.code in {"process_detection_failed", "resource_in_use"}:
            if issue.code in guard_codes:
                continue
            guard_codes.add(issue.code)
        issues.append(issue)
    combined.issues = issues
    empty = {item.path for item in combined.items if item.size == 0 and not item.cloud_file_count}
    return _deduplicate_hardlinks(combined), empty


def analyze_path(
    path: str | os.PathLike[str],
    *,
    protection: Predicate | None = None,
    control: Control | None = None,
    on_update: Callable[[AnalysisUpdate], None] | None = None,
    workers: int = 1,
    include_empty: bool = False,
) -> SpaceAnalysis:
    """统计指定目录的一级子项，子目录大小递归计算并按大小排序。"""
    root = normalize_path(path)
    protection = protection or IgnoreRules()
    control = control or Control()
    if workers < 1:
        raise ValueError("workers 必须大于 0")
    try:
        root_stat = lstat_retry(root)
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
    if root_facts.is_dataless:
        raise AnalyzeError(
            "为避免触发云端枚举或下载，拒绝分析 macOS dataless 根目录："
            f"{root}"
        )
    if protection.should_ignore(root_facts):
        raise AnalyzeError(f"分析路径命中忽略或保护规则：{root}")

    analysis = SpaceAnalysis(root=root, root_identity=(
        root_stat.st_dev, root_stat.st_ino, root_stat.st_uid, getattr(root_stat, "st_flags", 0),
    ))
    try:
        candidate_paths = []
        for entry in scandir_entries(root):
            control.checkpoint()
            candidate_paths.append(entry.path)
    except Cancelled:
        analysis.cancelled = True
        return analysis
    except OSError as exc:
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

    scan_result, empty_paths = _scan_candidates(
        candidate_paths, control, protection, workers, on_update, include_empty,
    )
    analysis.issues.extend(scan_result.issues)
    analysis.cancelled = scan_result.cancelled
    prepared_items: list[Item] = []
    for item in scan_result.items:
        prepared = replace(
            item,
            preselected=False,
            safety="critical",
            requires_explicit_selection=True,
        )
        if reason := nonprivileged_action_block_reason(item.path):
            prepared = replace(
                prepared,
                actionable=False,
                action_block_reason=reason,
            )
        prepared_items.append(prepared)
    scan_result.items = prepared_items
    total = scan_result.total
    analysis.browse_entries = [
        SpaceEntry(
            item=item,
            percent=(item.size / total * 100.0 if total else 0.0),
        )
        for item in sorted(scan_result.items, key=lambda candidate: -candidate.size)
    ]
    analysis.entries = [entry for entry in analysis.browse_entries if entry.item.path not in empty_paths]
    try:
        control.checkpoint()
    except Cancelled:
        analysis.cancelled = True
    if analysis.cancelled:
        return analysis

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
    except OSError as exc:
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
    try:
        control.checkpoint()
    except Cancelled:
        analysis.cancelled = True
    return analysis
