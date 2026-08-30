"""扫描引擎 · 独立实现（依据 specs/01-scan-engine.md）。

职责：目录大小统计（硬链接去重、符号链接不跟随）、并发、协作式取消、
忽略规则、聚合结果。全部为本项目原创实现。
"""
from __future__ import annotations

import fnmatch
import os
import stat
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

from .application_languages import scan_application_languages
from .application_ownership import process_markers_for_path
from .docker import scan_docker_resources
from .filesystem import filesystem_id_retry, lstat_retry, scandir_entries
from .knowledge_base import KnowledgeBase
from .macos import (
    discover_darwin_user_cache,
    discover_trash_paths,
    scan_symlink_anchor,
    symlink_component,
)
from .models import (
    FILESYSTEM_RESOURCE_KINDS,
    FileFacts,
    Item,
    ScanIssue,
    ScanResult,
    normalize_path,
)
from .predicates import Predicate, ProtectionGate, SubstringPathPredicate
from .processes import (
    ProcessDetectionError,
    ProcessSnapshot,
    capture_process_snapshot,
)
from .progress import (
    ProgressSnapshot,
    ProgressTaskSpec,
    TaskProgress,
    WeightedProgress,
)
from .scanpoints import (
    DEFAULT_PROJECT_ROOT_NAMES,
    DOMAINS,
    PROJECT_ARTIFACT_GLOBS,
    PROJECT_ARTIFACT_NAMES,
    PROJECT_MARKER_GLOBS,
    PROJECT_MARKER_NAMES,
    ScanPoint,
)
from .startup_items import scan_broken_startup_items
from .storage_diagnostics import (
    scan_codex_storage_artifact_diagnostics,
    scan_darwin_temp_updater_diagnostics,
    scan_open_unlinked_diagnostics,
    scan_retention_diagnostics,
    scan_sqlite_diagnostics,
)
from .task_graph import TaskSpec as GraphTaskSpec
from .task_graph import execute_task_graph
from .updater import assess_updater_candidate


class Cancelled(Exception):
    pass


class Control:
    """协作式控制：pause/resume/cancel（specs §控制）。"""
    def __init__(self):
        self._cancel = threading.Event()
        self._pause = threading.Event()
        self._pause.set()  # set = running

    def cancel(self):
        self._cancel.set()
        self._pause.set()

    def pause(self):
        self._pause.clear()

    def resume(self):
        self._pause.set()

    def checkpoint(self):
        self._pause.wait()          # 阻塞直到 resume
        if self._cancel.is_set():
            raise Cancelled()


class IgnoreRules(ProtectionGate):
    """兼容 ``--ignore`` 的保护闸；知识库规则始终最先判定。"""

    def __init__(
        self,
        patterns: list[str] | None = None,
        knowledge_base: KnowledgeBase | None = None,
    ) -> None:
        cleaned = tuple(pattern.strip() for pattern in (patterns or []) if pattern.strip())
        predicates = (SubstringPathPredicate(cleaned),) if cleaned else ()
        super().__init__(knowledge_base=knowledge_base, predicates=predicates)


def _predicates_ignore_after_knowledge_base(
    protection: Predicate,
    facts: FileFacts,
) -> bool:
    if isinstance(protection, ProtectionGate):
        return protection.predicates_ignore(facts)
    return protection.should_ignore(facts)


@dataclass
class _DirectoryMeasurement:
    size: int = 0
    logical_size: int = 0
    allocated_size: int = 0
    cloud_file_count: int = 0
    cloud_logical_size: int = 0
    latest_mtime: float = 0.0
    excluded_paths: int = 0
    cross_device_paths: int = 0


@dataclass(frozen=True)
class _ScanCandidate:
    facts: FileFacts
    root: FileFacts
    path_source: str = "builtin"


@dataclass(frozen=True)
class _ScanRoot:
    path: Path
    path_source: str = "builtin"


def _dir_size(
    root: Path,
    ctl: Control,
    seen_inodes: set[tuple[int, int]],
    protection: Predicate,
    issues: list[ScanIssue],
    task: str,
) -> int:
    """递归求和；硬链接按 inode 去重；符号链接不跟随到目标（防重复计数）。"""
    return _measure_dir(
        root, ctl, seen_inodes, protection, issues, task, None
    ).size


def _measure_dir(
    root: Path,
    ctl: Control,
    seen_inodes: set[tuple[int, int]],
    protection: Predicate,
    issues: list[ScanIssue],
    task: str,
    progress: TaskProgress | None = None,
    device_boundary: int | None = None,
) -> _DirectoryMeasurement:
    measurement = _DirectoryMeasurement()
    root_facts = _inspect_path(
        root,
        issues,
        task,
        protection,
        missing_is_issue=True,
    )
    if root_facts is None:
        measurement.excluded_paths += 1
        return measurement
    if stat.S_ISLNK(root_facts.stat.st_mode) or not stat.S_ISDIR(
        root_facts.stat.st_mode
    ):
        return measurement
    filesystem_boundary: int | None = None
    if device_boundary is not None:
        try:
            filesystem_boundary = filesystem_id_retry(root_facts.path)
        except (PermissionError, FileNotFoundError, OSError) as exc:
            _append_issue(issues, exc, root_facts.path, task)
            measurement.excluded_paths += 1
            return measurement
    directories = [root_facts]
    while directories:
        directory = directories.pop()
        current_directory = _inspect_path(
            directory.path,
            issues,
            task,
            protection,
            missing_is_issue=True,
        )
        if (
            current_directory is None
            or stat.S_ISLNK(current_directory.stat.st_mode)
            or not stat.S_ISDIR(current_directory.stat.st_mode)
        ):
            measurement.excluded_paths += 1
            continue
        directory = current_directory
        if device_boundary is not None:
            if directory.stat.st_dev != device_boundary:
                measurement.cross_device_paths += 1
                issues.append(
                    ScanIssue(
                        code="cross_device_skipped",
                        message="检测到其它文件系统挂载点，已停止跨卷遍历",
                        task=task,
                        path=directory.path,
                        blocking=False,
                    )
                )
                continue
            try:
                current_filesystem = filesystem_id_retry(directory.path)
            except (PermissionError, FileNotFoundError, OSError) as exc:
                _append_issue(issues, exc, directory.path, task)
                measurement.excluded_paths += 1
                continue
            if current_filesystem != filesystem_boundary:
                measurement.cross_device_paths += 1
                issues.append(
                    ScanIssue(
                        code="cross_device_skipped",
                        message="检测到其它文件系统挂载点，已停止跨卷遍历",
                        task=task,
                        path=directory.path,
                        blocking=False,
                    )
                )
                continue
        if directory.is_dataless:
            measurement.cloud_file_count += 1
            measurement.latest_mtime = max(
                measurement.latest_mtime,
                directory.stat.st_mtime,
            )
            continue
        try:
            for entry in scandir_entries(directory.path):
                ctl.checkpoint()
                if progress is not None:
                    progress.advance()
                if (
                    isinstance(protection, ProtectionGate)
                    and protection.knowledge_base_ignores(entry.path)
                ):
                    measurement.excluded_paths += 1
                    continue
                facts = _facts_from_entry(
                    entry,
                    issues,
                    task,
                )
                if facts is None:
                    measurement.excluded_paths += 1
                    continue
                if _predicates_ignore_after_knowledge_base(
                    protection, facts
                ):
                    measurement.excluded_paths += 1
                    continue
                if stat.S_ISLNK(facts.stat.st_mode):
                    continue
                if (
                    device_boundary is not None
                    and facts.stat.st_dev != device_boundary
                ):
                    measurement.cross_device_paths += 1
                    issues.append(
                        ScanIssue(
                            code="cross_device_skipped",
                            message="检测到其它文件系统挂载点，已停止跨卷遍历",
                            task=task,
                            path=facts.path,
                            blocking=False,
                        )
                    )
                    continue
                measurement.latest_mtime = max(
                    measurement.latest_mtime, facts.stat.st_mtime
                )
                if facts.is_probable_cloud_placeholder:
                    measurement.cloud_file_count += 1
                    if stat.S_ISREG(facts.stat.st_mode):
                        measurement.logical_size += facts.logical_size
                        measurement.cloud_logical_size += facts.logical_size
                elif stat.S_ISDIR(facts.stat.st_mode):
                    directories.append(facts)
                else:
                    key = (facts.stat.st_dev, facts.stat.st_ino)
                    if facts.stat.st_nlink > 1:
                        if key in seen_inodes:
                            continue
                        seen_inodes.add(key)
                    measurement.logical_size += facts.logical_size
                    measurement.allocated_size += facts.allocated_size
                    measurement.size += facts.allocated_size
        except (PermissionError, FileNotFoundError, OSError) as exc:
            _append_issue(issues, exc, directory.path, task)
            measurement.excluded_paths += 1
    return measurement


def _facts_from_entry(
    entry: os.DirEntry[str],
    issues: list[ScanIssue],
    task: str,
) -> FileFacts | None:
    path = Path(entry.path)
    try:
        stat_result = lstat_retry(path)
    except (PermissionError, FileNotFoundError, OSError) as exc:
        _append_issue(issues, exc, path, task)
        return None
    return FileFacts(path=path, stat=stat_result)


def _inspect_path(
    path: str | os.PathLike[str],
    issues: list[ScanIssue],
    task: str,
    protection: Predicate,
    *,
    missing_is_issue: bool,
) -> FileFacts | None:
    normalized = normalize_path(path)
    if (
        isinstance(protection, ProtectionGate)
        and protection.knowledge_base_ignores(normalized)
    ):
        return None
    component = symlink_component(
        normalized,
        anchor=scan_symlink_anchor(normalized),
    )
    if component is not None and component != normalized:
        issues.append(
            ScanIssue(
                code="unsafe_symlink_ancestor",
                message="候选路径包含符号链接组件，已拒绝扫描",
                task=task,
                path=component,
            )
        )
        return None
    try:
        facts = FileFacts(path=normalized, stat=lstat_retry(normalized))
    except FileNotFoundError as exc:
        if missing_is_issue:
            _append_issue(issues, exc, normalized, task)
        return None
    except (PermissionError, OSError) as exc:
        _append_issue(issues, exc, normalized, task)
        return None
    if _predicates_ignore_after_knowledge_base(protection, facts):
        return None
    return facts


def _append_issue(
    issues: list[ScanIssue],
    error: OSError,
    path: str | os.PathLike[str],
    task: str,
) -> None:
    if isinstance(error, PermissionError):
        code = "permission_denied"
    elif isinstance(error, FileNotFoundError):
        code = "path_disappeared"
    else:
        code = "filesystem_error"
    issues.append(
        ScanIssue(code=code, message=str(error), task=task, path=normalize_path(path))
    )


def _append_dataless_issue(
    issues: list[ScanIssue],
    path: Path,
    task: str,
) -> None:
    issues.append(
        ScanIssue(
            code="dataless_directory_skipped",
            message=(
                "检测到 macOS dataless 目录；"
                "为避免触发云端枚举或下载，已停止遍历"
            ),
            task=task,
            path=path,
        )
    )


_GLOB_MAGIC = frozenset("*?[")


def _has_glob_magic(component: str) -> bool:
    return any(character in component for character in _GLOB_MAGIC)


def _scan_point_weight(point: ScanPoint) -> int:
    return max(
        1,
        len(point.paths)
        + len(point.env_paths)
        + len(point.path_globs)
        + int(point.path_provider is not None),
    )


def _safe_glob_paths(
    pattern: str,
    ctl: Control,
    protection: Predicate,
    issues: list[ScanIssue],
    task: str,
    progress: TaskProgress | None,
) -> Iterator[Path]:
    """展开非递归路径 glob，且不穿过符号链接目录。"""
    expanded = os.path.abspath(os.path.expanduser(pattern))
    parts = Path(expanded).parts
    magic_index = next(
        (
            index
            for index, component in enumerate(parts)
            if _has_glob_magic(component)
        ),
        None,
    )
    if magic_index is None:
        yield normalize_path(expanded)
        return

    prefix = Path(*parts[:magic_index])
    prefix_facts = _inspect_path(
        prefix,
        issues,
        task,
        protection,
        missing_is_issue=False,
    )
    if (
        prefix_facts is None
        or stat.S_ISLNK(prefix_facts.stat.st_mode)
        or not stat.S_ISDIR(prefix_facts.stat.st_mode)
    ):
        return
    if prefix_facts.is_dataless:
        _append_dataless_issue(issues, prefix_facts.path, task)
        return

    candidates = [prefix_facts.path]
    final_index = len(parts) - 1
    for index in range(magic_index, len(parts)):
        component = parts[index]
        next_candidates: list[Path] = []
        for parent in candidates:
            ctl.checkpoint()
            if progress is not None:
                progress.advance()
            if _has_glob_magic(component):
                parent_facts = _inspect_path(
                    parent,
                    issues,
                    task,
                    protection,
                    missing_is_issue=True,
                )
                if (
                    parent_facts is None
                    or stat.S_ISLNK(parent_facts.stat.st_mode)
                    or not stat.S_ISDIR(parent_facts.stat.st_mode)
                ):
                    continue
                if parent_facts.is_dataless:
                    _append_dataless_issue(issues, parent_facts.path, task)
                    continue
                try:
                    entries = sorted(
                        scandir_entries(parent),
                        key=lambda entry: entry.name,
                    )
                except (PermissionError, FileNotFoundError, OSError) as exc:
                    _append_issue(issues, exc, parent, task)
                    continue
                for entry in entries:
                    ctl.checkpoint()
                    if progress is not None:
                        progress.advance()
                    if not fnmatch.fnmatchcase(entry.name, component):
                        continue
                    candidate = normalize_path(entry.path)
                    if (
                        isinstance(protection, ProtectionGate)
                        and protection.knowledge_base_ignores(candidate)
                    ):
                        continue
                    next_candidates.append(candidate)
            else:
                candidate = normalize_path(parent / component)
                if (
                    isinstance(protection, ProtectionGate)
                    and protection.knowledge_base_ignores(candidate)
                ):
                    continue
                next_candidates.append(candidate)

        if index < final_index:
            traversable: list[Path] = []
            for candidate in next_candidates:
                facts = _inspect_path(
                    candidate,
                    issues,
                    task,
                    protection,
                    missing_is_issue=False,
                )
                if (
                    facts is None
                    or stat.S_ISLNK(facts.stat.st_mode)
                    or not stat.S_ISDIR(facts.stat.st_mode)
                ):
                    continue
                if facts.is_dataless:
                    _append_dataless_issue(issues, facts.path, task)
                    continue
                traversable.append(facts.path)
            candidates = traversable
        else:
            candidates = next_candidates
        if not candidates:
            break

    yield from candidates


def _child_matches(point: ScanPoint, name: str) -> bool:
    if point.child_globs and not any(
        fnmatch.fnmatchcase(name, pattern) for pattern in point.child_globs
    ):
        return False
    suffix = Path(name).suffix.casefold()
    return not point.child_extensions or any(
        suffix == extension.casefold()
        for extension in point.child_extensions
    )


def _scan_point_roots(
    point: ScanPoint,
    ctl: Control,
    protection: Predicate,
    issues: list[ScanIssue],
    progress: TaskProgress | None,
) -> Iterator[_ScanRoot]:
    for raw_path in point.paths:
        ctl.checkpoint()
        if progress is not None:
            progress.advance()
        yield _ScanRoot(normalize_path(raw_path))
    trusted_environment_roots = (
        normalize_path(Path.home() / "Library" / "Caches"),
        normalize_path(Path.home() / ".cache"),
    )
    for name in point.env_paths:
        value = os.environ.get(name)
        if not value:
            continue
        ctl.checkpoint()
        if progress is not None:
            progress.advance()
        environment_path = normalize_path(value)
        if not any(
            environment_path == root
            or _is_descendant(environment_path, root)
            for root in trusted_environment_roots
        ):
            issues.append(
                ScanIssue(
                    code="unsafe_environment_path",
                    message=(
                        f"{name} 指向可信用户缓存根之外，已拒绝扫描和清理"
                    ),
                    task=point.category,
                    path=environment_path,
                )
            )
            continue
        yield _ScanRoot(environment_path, "environment")
    for pattern in point.path_globs:
        ctl.checkpoint()
        if progress is not None:
            progress.advance()
        yield from (
            _ScanRoot(path)
            for path in _safe_glob_paths(
                pattern,
                ctl,
                protection,
                issues,
                point.category,
                progress,
            )
        )
    if point.path_provider is not None:
        ctl.checkpoint()
        if progress is not None:
            progress.advance()
        if point.path_provider == "darwin-user-cache":
            discovery = discover_darwin_user_cache()
            issues.extend(discovery.issues)
            yield from (_ScanRoot(path) for path in discovery.paths)
        else:
            issues.append(
                ScanIssue(
                    code="path_discovery_failed",
                    message=f"未知路径发现器：{point.path_provider}",
                    task=point.category,
                )
            )


def _scan_point_candidates(
    point: ScanPoint,
    ctl: Control,
    protection: Predicate,
    issues: list[ScanIssue],
    progress: TaskProgress | None,
) -> Iterator[_ScanCandidate]:
    seen_roots: set[Path] = set()
    seen_candidates: set[Path] = set()
    for scan_root in _scan_point_roots(
        point, ctl, protection, issues, progress
    ):
        root = scan_root.path
        if root in seen_roots:
            continue
        seen_roots.add(root)
        root_facts = _inspect_path(
            root,
            issues,
            point.category,
            protection,
            missing_is_issue=False,
        )
        if (
            root_facts is None
            or stat.S_ISLNK(root_facts.stat.st_mode)
        ):
            continue
        if not point.expand_children:
            if root_facts.path not in seen_candidates:
                seen_candidates.add(root_facts.path)
                yield _ScanCandidate(
                    facts=root_facts,
                    root=root_facts,
                    path_source=scan_root.path_source,
                )
            continue
        if root_facts.is_dataless:
            if root_facts.path not in seen_candidates:
                seen_candidates.add(root_facts.path)
                yield _ScanCandidate(
                    facts=root_facts,
                    root=root_facts,
                    path_source=scan_root.path_source,
                )
            continue
        if not stat.S_ISDIR(root_facts.stat.st_mode):
            continue
        try:
            entries = sorted(
                scandir_entries(root_facts.path),
                key=lambda entry: entry.name,
            )
        except (PermissionError, FileNotFoundError, OSError) as exc:
            _append_issue(issues, exc, root_facts.path, point.category)
            continue
        for entry in entries:
            ctl.checkpoint()
            if progress is not None:
                progress.advance()
            candidate = normalize_path(entry.path)
            if candidate in seen_candidates:
                continue
            if not _child_matches(point, entry.name):
                continue
            facts = _inspect_path(
                candidate,
                issues,
                point.category,
                protection,
                missing_is_issue=True,
            )
            if (
                facts is None
                or stat.S_ISLNK(facts.stat.st_mode)
            ):
                continue
            if point.child_extensions and not stat.S_ISREG(facts.stat.st_mode):
                continue
            seen_candidates.add(facts.path)
            yield _ScanCandidate(
                facts=facts,
                root=root_facts,
                path_source=scan_root.path_source,
            )


def _scan_point(
    sp: ScanPoint,
    ctl: Control,
    protection: Predicate,
    progress: TaskProgress | None = None,
    process_snapshot: ProcessSnapshot | None = None,
) -> ScanResult:
    result = ScanResult()
    reported_resource_in_use = False
    seen: set[tuple[int, int]] = set()
    for candidate in _scan_point_candidates(
        sp, ctl, protection, result.issues, progress
    ):
        facts = candidate.facts
        owner_markers = (
            process_markers_for_path(
                facts.path,
                darwin_cache_root=(
                    candidate.root.path
                    if sp.path_provider == "darwin-user-cache"
                    else None
                ),
            )
            if sp.process_owner_protection
            else ()
        )
        process_markers = tuple(
            dict.fromkeys((*sp.running_process_markers, *owner_markers))
        )
        process_state_unknown = bool(process_markers) and process_snapshot is None
        resource_in_use = bool(
            process_markers
            and process_snapshot is not None
            and process_snapshot.any_running(process_markers)
        )
        if resource_in_use and not reported_resource_in_use:
            result.issues.append(
                ScanIssue(
                    code="resource_in_use",
                    message="检测到相关工具正在运行，候选仅报告且不可执行",
                    task=sp.category,
                    blocking=False,
                )
            )
            reported_resource_in_use = True
        updater = (
            assess_updater_candidate(facts.path)
            if sp.updater_protection
            else None
        )
        is_cloud_file = facts.is_probable_cloud_placeholder
        if is_cloud_file:
            logical_size = (
                facts.logical_size if stat.S_ISREG(facts.stat.st_mode) else 0
            )
            allocated_size = 0
            size = 0
            cloud_file_count = 1
            cloud_logical_size = logical_size
            excluded_paths = 0
            cross_device_paths = 0
        elif stat.S_ISDIR(facts.stat.st_mode):
            measurement = _measure_dir(
                facts.path,
                ctl,
                seen,
                protection,
                result.issues,
                sp.category,
                progress,
                facts.stat.st_dev if sp.stay_on_device else None,
            )
            size = measurement.size
            logical_size = measurement.logical_size
            allocated_size = measurement.allocated_size
            cloud_file_count = measurement.cloud_file_count
            cloud_logical_size = measurement.cloud_logical_size
            is_cloud_file = False
            excluded_paths = measurement.excluded_paths
            cross_device_paths = measurement.cross_device_paths
        else:
            is_cloud_file = False
            logical_size = facts.logical_size
            allocated_size = 0 if is_cloud_file else facts.allocated_size
            size = allocated_size
            cloud_file_count = 1 if is_cloud_file else 0
            cloud_logical_size = logical_size if is_cloud_file else 0
            excluded_paths = 0
            cross_device_paths = 0
        if size > 0 or cloud_file_count > 0:
            note = sp.note
            if excluded_paths:
                suffix = f"包含 {excluded_paths} 个忽略/保护路径，默认不选"
                note = f"{note}；{suffix}" if note else suffix
            if cross_device_paths:
                suffix = (
                    f"包含 {cross_device_paths} 个跨卷挂载路径，"
                    "未计入容量且不可执行"
                )
                note = f"{note}；{suffix}" if note else suffix
            if cloud_file_count:
                suffix = (
                    f"包含 {cloud_file_count} 个云占位文件（逻辑大小 "
                    f"{human(cloud_logical_size)}），不计入可回收容量且默认不选"
                )
                note = f"{note}；{suffix}" if note else suffix
            environment_override = candidate.path_source == "environment"
            safety = (
                "critical"
                if cloud_file_count or updater is not None
                else "confirm"
                if environment_override and sp.safety == "safe"
                else sp.safety
            )
            if environment_override:
                suffix = (
                    "来自环境变量覆盖路径；不会批量选择，执行必须逐项明确选择"
                )
                note = f"{note}；{suffix}" if note else suffix
            if sp.requires_privilege and "特权帮助器" not in note:
                suffix = "需要尚未实现的特权帮助器，当前仅只读报告"
                note = f"{note}；{suffix}" if note else suffix
            if resource_in_use:
                suffix = "相关应用正在运行；当前仅报告，退出后需重新扫描"
                note = f"{note}；{suffix}" if note else suffix
            elif process_state_unknown:
                suffix = "无法确认相关应用是否正在运行；当前仅报告"
                note = f"{note}；{suffix}" if note else suffix
            if updater is not None:
                note = f"{note}；{updater.note}" if note else updater.note
            updater_blocked = updater is not None and updater.blocks_cleanup
            actionable = (
                excluded_paths == 0
                and cross_device_paths == 0
                and cloud_file_count == 0
                and not sp.requires_privilege
                and not resource_in_use
                and not process_state_unknown
                and not updater_blocked
            )
            if excluded_paths:
                action_block_reason = "包含忽略或保护路径"
            elif cross_device_paths:
                action_block_reason = "包含跨卷挂载路径"
            elif cloud_file_count:
                action_block_reason = "包含云占位文件"
            elif sp.requires_privilege:
                action_block_reason = "需要尚未实现的特权帮助器"
            elif updater_blocked:
                assert updater is not None
                action_block_reason = updater.block_reason
            elif resource_in_use:
                action_block_reason = "相关应用正在运行"
            elif process_state_unknown:
                action_block_reason = "无法确认相关应用是否正在运行"
            else:
                action_block_reason = ""
            default_selected = (
                safety == "safe"
                if sp.default_selected is None
                else sp.default_selected
            )
            result.items.append(
                Item(
                    facts.path,
                    size,
                    sp.category,
                    safety,
                    note,
                    logical_size=logical_size,
                    allocated_size=allocated_size,
                    is_cloud_file=is_cloud_file,
                    cloud_file_count=cloud_file_count,
                    cloud_logical_size=cloud_logical_size,
                    actionable=actionable,
                    action_block_reason=action_block_reason,
                    requires_privilege=sp.requires_privilege,
                    identity=facts.identity,
                    preselected=(
                        default_selected
                        and actionable
                        and size > 0
                    ),
                    excluded_paths=excluded_paths,
                    cross_device_paths=cross_device_paths,
                    domain=sp.domain,
                    path_source=candidate.path_source,
                    requires_explicit_selection=(
                        environment_override or updater is not None
                    ),
                    running_process_markers=process_markers,
                    cleanup_scope=(
                        "darwin-user-cache"
                        if sp.path_provider == "darwin-user-cache"
                        else ""
                    ),
                    cleanup_root=(
                        candidate.root.path
                        if sp.path_provider == "darwin-user-cache"
                        else None
                    ),
                    cleanup_root_identity=(
                        candidate.root.identity
                        if sp.path_provider == "darwin-user-cache"
                        else None
                    ),
                    updater_status=updater.status if updater else "",
                    installed_version=(
                        updater.installed_version if updater else ""
                    ),
                    staged_version=(
                        updater.staged_version if updater else ""
                    ),
                    updater_external_install=(
                        updater.external_install if updater else False
                    ),
                )
            )
    return result


def _process_snapshot_for_points(
    points: list[ScanPoint],
) -> tuple[ProcessSnapshot | None, ScanIssue | None]:
    if not any(
        point.running_process_markers or point.process_owner_protection
        for point in points
    ):
        return None, None
    try:
        return capture_process_snapshot(), None
    except ProcessDetectionError as exc:
        return None, ScanIssue(
            code="process_detection_failed",
            message=str(exc),
            task="running-process-protection",
        )


def _scan_dynamic_point_with_progress(
    point: ScanPoint,
    ctl: Control,
    protection: Predicate,
    progress: TaskProgress,
) -> ScanResult:
    try:
        ctl.checkpoint()
        progress.advance()
        if point.scanner == "docker":
            result = scan_docker_resources()
        elif point.scanner == "broken-startup-items":
            result = scan_broken_startup_items(
                point.paths,
                protection,
                category=point.category,
                safety=point.safety,
                checkpoint=ctl.checkpoint,
                on_progress=progress.advance,
            )
        elif point.scanner == "application-languages":
            result = scan_application_languages(
                point.paths,
                protection,
                category=point.category,
                context_note=point.note,
                checkpoint=ctl.checkpoint,
                on_progress=progress.advance,
            )
        elif point.scanner == "retention-diagnostics":
            result = scan_retention_diagnostics(protection)
        elif point.scanner == "darwin-temp-updater":
            result = scan_darwin_temp_updater_diagnostics(protection)
        elif point.scanner == "sqlite-freelist":
            result = scan_sqlite_diagnostics(protection)
        elif point.scanner == "open-unlinked":
            result = scan_open_unlinked_diagnostics(protection)
        elif point.scanner == "codex-storage-artifacts":
            result = scan_codex_storage_artifact_diagnostics(protection)
        else:
            result = ScanResult(
                issues=[
                    ScanIssue(
                        code="scanner_unavailable",
                        message=f"未知动态扫描器：{point.scanner}",
                        task=point.category,
                    )
                ]
            )
        ctl.checkpoint()
    except Cancelled:
        progress.cancel()
        ctl.cancel()
        raise
    except Exception:
        progress.fail()
        raise
    else:
        progress.complete()
        return result


def scan_domains(domains: list[str], ctl: Control | None = None,
                 ignore: Predicate | None = None,
                 workers: int = 8,
                 on_progress: Callable[[ProgressSnapshot], None] | None = None,
                 ) -> ScanResult:
    """扫描指定域（specs §扫描器协议：配置→可清理项流）。"""
    ctl = ctl or Control()
    ignore = ignore or IgnoreRules()
    points: list[ScanPoint] = []
    dynamic_points: list[ScanPoint] = []
    setup_issues: list[ScanIssue] = []
    for d in domains:
        domain_points = DOMAINS.get(d, [])
        dynamic_trash_points = [
            point for point in domain_points if point.category == "废纸篓"
        ]
        if d == "trash" and dynamic_trash_points:
            discovery = discover_trash_paths()
            setup_issues.extend(discovery.issues)
            dynamic_paths = tuple(str(path) for path in discovery.paths)
            assembled = [
                replace(
                    point,
                    paths=(dynamic_paths if point.category == "废纸篓" else point.paths),
                    domain=d,
                )
                for point in domain_points
            ]
        else:
            assembled = [replace(point, domain=d) for point in domain_points]
        points.extend(point for point in assembled if point.scanner is None)
        dynamic_points.extend(
            point for point in assembled if point.scanner is not None
        )
    seen_scanners: set[tuple[str, str]] = set()
    unique_dynamic: list[ScanPoint] = []
    for point in dynamic_points:
        scanner_key = (point.domain, point.scanner or "")
        if scanner_key not in seen_scanners:
            seen_scanners.add(scanner_key)
            unique_dynamic.append(point)

    process_snapshot, process_issue = _process_snapshot_for_points(points)
    if process_issue is not None:
        setup_issues.append(process_issue)

    filesystem_ids = [f"filesystem:{index}" for index in range(len(points))]
    dynamic_ids = [
        f"dynamic:{index}" for index in range(len(unique_dynamic))
    ]
    progress = WeightedProgress(
        [
            ProgressTaskSpec(
                task_id,
                point.category,
                _scan_point_weight(point),
            )
            for task_id, point in zip(filesystem_ids, points, strict=True)
        ]
        + [
            ProgressTaskSpec(task_id, point.category, 1)
            for task_id, point in zip(
                dynamic_ids, unique_dynamic, strict=True
            )
        ],
        callback=on_progress,
    )
    progress.start()
    graph_specs = [
        GraphTaskSpec(
            task_id,
            lambda point=point, task_id=task_id: _scan_point_with_progress(
                point,
                ctl,
                ignore,
                progress.task(task_id),
                process_snapshot,
            ),
        )
        for task_id, point in zip(filesystem_ids, points, strict=True)
    ]
    graph_specs.extend(
        GraphTaskSpec(
            task_id,
            lambda point=point, task_id=task_id: (
                _scan_dynamic_point_with_progress(
                    point,
                    ctl,
                    ignore,
                    progress.task(task_id),
                )
            ),
        )
        for task_id, point in zip(dynamic_ids, unique_dynamic, strict=True)
    )
    task_points = {
        task_id: point
        for task_id, point in zip(filesystem_ids, points, strict=True)
    }
    task_points.update(
        {
            task_id: point
            for task_id, point in zip(
                dynamic_ids, unique_dynamic, strict=True
            )
        }
    )
    dynamic_id_set = set(dynamic_ids)
    graph_result = execute_task_graph(graph_specs, workers=workers)
    result = ScanResult(issues=list(setup_issues))
    for outcome in graph_result.outcomes:
        point = task_points[outcome.identifier]
        if outcome.blocked_by:
            result.issues.append(
                ScanIssue(
                    code="dependency_failed",
                    message=(
                        "任务依赖失败：" + ", ".join(outcome.blocked_by)
                    ),
                    task=point.category,
                )
            )
            continue
        if outcome.error is not None:
            if isinstance(outcome.error, Cancelled):
                result.cancelled = True
                continue
            result.issues.append(
                ScanIssue(
                    code="task_failed",
                    message=(
                        f"{type(outcome.error).__name__}: {outcome.error}"
                    ),
                    task=point.category,
                )
            )
            continue
        assert outcome.value is not None
        task_result = outcome.value
        if outcome.identifier in dynamic_id_set:
            result.items.extend(
                replace(item, domain=point.domain)
                for item in task_result.items
            )
        else:
            result.items.extend(task_result.items)
        result.issues.extend(task_result.issues)
        result.cancelled = result.cancelled or task_result.cancelled
    if result.cancelled:
        ctl.cancel()
        progress.cancel()
    return result


def _scan_point_with_progress(
    point: ScanPoint,
    ctl: Control,
    ignore: Predicate,
    progress: TaskProgress,
    process_snapshot: ProcessSnapshot | None,
) -> ScanResult:
    try:
        result = _scan_point(
            point, ctl, ignore, progress, process_snapshot
        )
    except Cancelled:
        progress.cancel()
        ctl.cancel()
        raise
    except Exception:
        progress.fail()
        raise
    else:
        progress.complete()
        return result


def _scan_points_with_progress(
    points: list[ScanPoint],
    ctl: Control,
    ignore: Predicate,
    workers: int,
    progress: WeightedProgress,
    task_ids: list[str],
    process_snapshot: ProcessSnapshot | None,
) -> ScanResult:
    result = ScanResult()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(
                _scan_point_with_progress,
                point,
                ctl,
                ignore,
                progress.task(task_id),
                process_snapshot,
            ): point
            for point, task_id in zip(points, task_ids, strict=True)
        }
        for future, scan_point in futures.items():
            try:
                task_result = future.result()
                result.items.extend(task_result.items)
                result.issues.extend(task_result.issues)
            except Cancelled:
                result.cancelled = True
                ctl.cancel()
                progress.cancel()
                break
            except Exception as exc:  # noqa: BLE001 - isolate worker failures
                result.issues.append(
                    ScanIssue(
                        code="task_failed",
                        message=f"{type(exc).__name__}: {exc}",
                        task=scan_point.category,
                    )
                )
    return result


def scan_points(
    points: list[ScanPoint],
    ctl: Control | None = None,
    ignore: Predicate | None = None,
    workers: int = 8,
    on_progress: Callable[[ProgressSnapshot], None] | None = None,
) -> ScanResult:
    """扫描显式扫描点集合，供域装配和集成测试共用。"""
    ctl = ctl or Control()
    ignore = ignore or IgnoreRules()
    task_ids = [f"filesystem:{index}" for index in range(len(points))]
    progress = WeightedProgress(
        (
            ProgressTaskSpec(
                task_id,
                point.category,
                _scan_point_weight(point),
            )
            for task_id, point in zip(task_ids, points, strict=True)
        ),
        callback=on_progress,
    )
    progress.start()
    process_snapshot, process_issue = _process_snapshot_for_points(points)
    result = _scan_points_with_progress(
        points,
        ctl,
        ignore,
        workers,
        progress,
        task_ids,
        process_snapshot,
    )
    if process_issue is not None:
        result.issues.insert(0, process_issue)
    return result


_DOMAIN_SPECIFICITY = {
    "system": 10,
    "project": 20,
    "developer": 30,
    "ai": 30,
    "trash": 30,
}

def _overlap_ownership_priority(item: Item) -> tuple[int, bool]:
    """同路径先按域具体度归属，同域时优先保留专用只读诊断。"""

    return (
        _DOMAIN_SPECIFICITY.get(item.domain, 0),
        bool(item.diagnostic_kind),
    )


def _is_descendant(candidate: Path, root: Path) -> bool:
    if candidate == root:
        return False
    try:
        return os.path.commonpath((str(candidate), str(root))) == str(root)
    except ValueError:
        return False


def _residual_metric(
    item: Item,
    descendants: list[Item],
    field: str,
) -> int | None:
    parent_value = getattr(item, field)
    child_values = [getattr(child, field) for child in descendants]
    if parent_value is None or any(value is None for value in child_values):
        return None
    return max(0, parent_value - sum(child_values))


def _retention_residual_updates(
    item: Item,
    descendants: list[Item],
) -> dict[str, object]:
    if item.diagnostic_kind != "retention":
        return {}
    updates: dict[str, object] = {
        "retention_file_count": _residual_metric(
            item, descendants, "retention_file_count"
        ),
        "open_handle_count": _residual_metric(
            item, descendants, "open_handle_count"
        ),
        "retention_7d_bytes": _residual_metric(
            item, descendants, "retention_7d_bytes"
        ),
        "retention_14d_bytes": _residual_metric(
            item, descendants, "retention_14d_bytes"
        ),
        "retention_30d_bytes": _residual_metric(
            item, descendants, "retention_30d_bytes"
        ),
        "latest_mtime": None,
        "age_days": None,
    }
    buckets = tuple(
        updates[field]
        for field in (
            "retention_7d_bytes",
            "retention_14d_bytes",
            "retention_30d_bytes",
        )
    )
    if all(value is not None for value in buckets) and not (
        buckets[0] >= buckets[1] >= buckets[2]
    ):
        updates.update(
            retention_7d_bytes=None,
            retention_14d_bytes=None,
            retention_30d_bytes=None,
        )
    return updates


def finalize_overlapping_result(result: ScanResult) -> ScanResult:
    """把重叠扫描点分配给最具体项，避免 clean 汇总重复计数。"""
    by_path: dict[Path, Item] = {}
    non_overlapping_items: list[Item] = []
    for item in result.items:
        if (
            item.path is None
            or item.resource_kind not in FILESYSTEM_RESOURCE_KINDS
        ):
            non_overlapping_items.append(item)
            continue
        current = by_path.get(item.path)
        if current is None or _overlap_ownership_priority(
            item
        ) > _overlap_ownership_priority(current):
            by_path[item.path] = item

    items = [
        item
        for item in by_path.values()
        if item.resource_kind == "filesystem"
    ]
    subset_items = [
        item
        for item in by_path.values()
        if item.resource_kind == "filesystem_subset"
    ]
    finalized: list[Item] = []
    for item in items:
        descendants = sorted(
            (
                candidate
                for candidate in items
                if _is_descendant(candidate.path, item.path)
            ),
            key=lambda candidate: len(candidate.path.parts),
        )
        direct_descendants: list[Item] = []
        for candidate in descendants:
            if any(
                _is_descendant(candidate.path, parent.path)
                for parent in direct_descendants
            ):
                continue
            direct_descendants.append(candidate)

        if not direct_descendants:
            finalized.append(item)
            continue
        residual_size = max(
            0, item.size - sum(child.size for child in direct_descendants)
        )
        residual_logical_size = max(
            0,
            (item.logical_size if item.logical_size is not None else item.size)
            - sum(
                child.logical_size
                if child.logical_size is not None
                else child.size
                for child in direct_descendants
            ),
        )
        residual_allocated_size = max(
            0,
            (
                item.allocated_size
                if item.allocated_size is not None
                else item.size
            )
            - sum(
                child.allocated_size
                if child.allocated_size is not None
                else child.size
                for child in direct_descendants
            ),
        )
        residual_cloud_file_count = max(
            0,
            item.cloud_file_count
            - sum(child.cloud_file_count for child in direct_descendants),
        )
        residual_cloud_logical_size = max(
            0,
            item.cloud_logical_size
            - sum(child.cloud_logical_size for child in direct_descendants),
        )
        if residual_size == 0 and residual_cloud_file_count == 0:
            continue
        suffix = (
            f"{len(direct_descendants)} 个子路径已归入更具体分类，父项默认不选"
        )
        note = f"{item.note}；{suffix}" if item.note else suffix
        residual_updates = _retention_residual_updates(
            item,
            direct_descendants,
        )
        finalized.append(
            replace(
                item,
                size=residual_size,
                logical_size=residual_logical_size,
                allocated_size=residual_allocated_size,
                cloud_file_count=residual_cloud_file_count,
                cloud_logical_size=residual_cloud_logical_size,
                actionable=False,
                action_block_reason="与更具体的清理分类重叠",
                preselected=False,
                note=note,
                **residual_updates,
            )
        )

    finalized.extend(subset_items)
    finalized.extend(non_overlapping_items)
    finalized.sort(
        key=lambda item: (
            item.domain,
            item.resource_kind,
            str(item.path) if item.path is not None else item.identifier,
        )
    )
    return ScanResult(
        items=finalized,
        issues=list(result.issues),
        cancelled=result.cancelled,
    )


def default_project_search_roots(home: Path | None = None) -> list[Path]:
    base = home or Path.home()
    return [base / name for name in DEFAULT_PROJECT_ROOT_NAMES]


def _matches_name(name: str, exact: tuple[str, ...], globs: tuple[str, ...]) -> bool:
    return name in exact or any(fnmatch.fnmatchcase(name, pattern) for pattern in globs)


def _is_artifact_name(name: str) -> bool:
    return _matches_name(name, PROJECT_ARTIFACT_NAMES, PROJECT_ARTIFACT_GLOBS)


def _has_project_marker(names: set[str]) -> bool:
    return any(
        _matches_name(name, PROJECT_MARKER_NAMES, PROJECT_MARKER_GLOBS)
        for name in names
    )


def _directory_entries(
    directory: Path,
    result: ScanResult,
    task: str,
) -> list[os.DirEntry[str]] | None:
    try:
        directory_stat = lstat_retry(directory)
        directory_facts = FileFacts(path=directory, stat=directory_stat)
        if directory_facts.is_dataless:
            _append_dataless_issue(result.issues, directory, task)
            return None
        if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(
            directory_stat.st_mode
        ):
            result.issues.append(
                ScanIssue(
                    code="unsafe_directory_changed",
                    message="待遍历路径已不再是普通目录，已停止遍历",
                    task=task,
                    path=directory,
                )
            )
            return None
        return list(scandir_entries(directory_facts.path))
    except (PermissionError, FileNotFoundError, OSError) as exc:
        _append_issue(result.issues, exc, directory, task)
        return None


def _discover_project_roots(
    search_roots: list[Path],
    ctl: Control,
    protection: Predicate,
    result: ScanResult,
    max_depth: int,
    include_unmarked_roots: bool,
    progress: TaskProgress | None = None,
) -> set[Path]:
    projects: set[Path] = set()

    def visit(directory: Path, depth: int, is_search_root: bool) -> None:
        if depth > max_depth:
            return
        ctl.checkpoint()
        if progress is not None:
            progress.advance()
        facts = _inspect_path(
            directory,
            result.issues,
            "project-discovery",
            protection,
            missing_is_issue=depth > 0,
        )
        if (
            facts is None
            or not stat.S_ISDIR(facts.stat.st_mode)
        ):
            return
        if facts.is_dataless:
            _append_dataless_issue(
                result.issues, facts.path, "project-discovery"
            )
            return

        entries = _directory_entries(facts.path, result, "project-discovery")
        if entries is None:
            return
        names = {entry.name for entry in entries}
        if _has_project_marker(names) or (
            include_unmarked_roots
            and is_search_root
            and any(_is_artifact_name(name) for name in names)
        ):
            projects.add(facts.path)

        for entry in entries:
            if progress is not None:
                progress.advance()
            if entry.name == ".git" or _is_artifact_name(entry.name):
                continue
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except (PermissionError, FileNotFoundError, OSError) as exc:
                _append_issue(
                    result.issues, exc, entry.path, "project-discovery"
                )
                continue
            visit(Path(entry.path), depth + 1, False)

    for root in search_roots:
        visit(Path(root).expanduser(), 0, True)
    return projects


def scan_project_artifacts(
    roots: list[Path],
    ctl: Control | None = None,
    ignore: Predicate | None = None,
    max_depth: int = 6,
    *,
    include_unmarked_roots: bool = False,
    preselect_age_days: int = 7,
    now: float | None = None,
    on_progress: Callable[[ProgressSnapshot], None] | None = None,
) -> ScanResult:
    """按项目分组扫描可重建产物，并标记七天以上的默认预选项。"""
    if max_depth < 0:
        raise ValueError("max_depth 不能为负数")
    if preselect_age_days < 0:
        raise ValueError("preselect_age_days 不能为负数")

    ctl = ctl or Control()
    ignore = ignore or IgnoreRules()
    result = ScanResult()
    progress = WeightedProgress(
        (
            ProgressTaskSpec("project-discovery", "发现项目", 1),
            ProgressTaskSpec("project-artifacts", "扫描项目产物", 3),
        ),
        callback=on_progress,
    )
    progress.start()
    discovery_progress = progress.task("project-discovery")
    artifact_progress = progress.task("project-artifacts")
    seen: set[tuple[int, int]] = set()
    reference_time = time.time() if now is None else now
    preselect_seconds = preselect_age_days * 24 * 60 * 60
    try:
        project_roots = _discover_project_roots(
            roots,
            ctl,
            ignore,
            result,
            max_depth,
            include_unmarked_roots,
            discovery_progress,
        )
    except Cancelled:
        result.cancelled = True
        discovery_progress.cancel()
        progress.cancel()
        return result
    except Exception:
        discovery_progress.fail()
        raise
    else:
        discovery_progress.complete()

    def walk_project(project_root: Path, directory: Path, depth: int) -> None:
        if depth > max_depth:
            return
        ctl.checkpoint()
        artifact_progress.advance()
        facts = _inspect_path(
            directory,
            result.issues,
            "project-artifacts",
            ignore,
            missing_is_issue=depth > 0,
        )
        if (
            facts is None
            or not stat.S_ISDIR(facts.stat.st_mode)
        ):
            return
        if facts.is_dataless:
            _append_dataless_issue(
                result.issues, facts.path, "project-artifacts"
            )
            return

        entries = _directory_entries(facts.path, result, "project-artifacts")
        if entries is None:
            return
        for entry in entries:
            ctl.checkpoint()
            artifact_progress.advance()
            child_path = normalize_path(entry.path)
            if child_path != project_root and child_path in project_roots:
                continue
            child_facts = _inspect_path(
                child_path,
                result.issues,
                "project-artifacts",
                ignore,
                missing_is_issue=True,
            )
            if child_facts is None:
                continue
            if not stat.S_ISDIR(child_facts.stat.st_mode):
                continue
            if _is_artifact_name(entry.name):
                if child_facts.is_dataless:
                    measurement = _DirectoryMeasurement(
                        cloud_file_count=1,
                        latest_mtime=child_facts.stat.st_mtime,
                    )
                else:
                    measurement = _measure_dir(
                        child_facts.path,
                        ctl,
                        seen,
                        ignore,
                        result.issues,
                        "project-artifacts",
                        artifact_progress,
                    )
                if measurement.size <= 0 and measurement.cloud_file_count == 0:
                    continue
                latest_mtime = max(
                    child_facts.stat.st_mtime, measurement.latest_mtime
                )
                age_seconds = max(0.0, reference_time - latest_mtime)
                age_days = int(age_seconds // (24 * 60 * 60))
                preselected = (
                    age_seconds >= preselect_seconds
                    and measurement.excluded_paths == 0
                    and measurement.cloud_file_count == 0
                )
                if measurement.excluded_paths:
                    selection_note = (
                        f"包含 {measurement.excluded_paths} 个忽略/保护路径，默认不选"
                    )
                elif measurement.cloud_file_count:
                    selection_note = (
                        f"包含 {measurement.cloud_file_count} 个云占位文件（逻辑大小 "
                        f"{human(measurement.cloud_logical_size)}），"
                        "不计入可回收容量且默认不选"
                    )
                elif preselected:
                    selection_note = f"超过 {preselect_age_days} 天，默认预选"
                else:
                    selection_note = f"最近 {preselect_age_days} 天内，默认不选"
                result.items.append(
                    Item(
                        child_facts.path,
                        measurement.size,
                        f"构建产物({entry.name})",
                        (
                            "critical"
                            if measurement.cloud_file_count
                            else "safe"
                        ),
                        f"可重新生成；{selection_note}",
                        logical_size=measurement.logical_size,
                        allocated_size=measurement.allocated_size,
                        cloud_file_count=measurement.cloud_file_count,
                        cloud_logical_size=measurement.cloud_logical_size,
                        actionable=(
                            measurement.excluded_paths == 0
                            and measurement.cloud_file_count == 0
                        ),
                        action_block_reason=(
                            "包含忽略或保护路径"
                            if measurement.excluded_paths
                            else "包含云占位文件"
                            if measurement.cloud_file_count
                            else ""
                        ),
                        identity=child_facts.identity,
                        project_root=project_root,
                        artifact_name=entry.name,
                        latest_mtime=latest_mtime,
                        age_days=age_days,
                        preselected=preselected,
                        excluded_paths=measurement.excluded_paths,
                        domain="project",
                    )
                )
                continue
            if child_facts.is_dataless:
                continue
            if entry.name == ".git":
                continue
            walk_project(project_root, child_facts.path, depth + 1)

    try:
        for project_root in sorted(project_roots, key=str):
            walk_project(project_root, project_root, 0)
    except Cancelled:
        result.cancelled = True
        artifact_progress.cancel()
        progress.cancel()
    except Exception:
        artifact_progress.fail()
        raise
    else:
        artifact_progress.complete()
    result.items.sort(key=lambda item: (str(item.project_root), str(item.path)))
    return result


def human(n: float) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.1f}{unit}" if unit != "B" else f"{int(f)}B"
        f /= 1024
    return f"{int(f)}B"
