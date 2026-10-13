"""已验证应用布局中的旧组件版本，只读报告，不修改签名应用。"""

from __future__ import annotations

import os
import plistlib
import stat
from collections.abc import Callable, Iterable
from pathlib import Path

from .filesystem import filesystem_id_retry
from .models import FileFacts, FileIdentity, Item, ScanIssue, ScanResult, normalize_path
from .path_inspection import PathInspection
from .predicates import Predicate, ProtectionGate
from .resource_walk import WalkBudget, open_directory, walk_resources
from .updater import _compare_versions, _read_app_metadata, _version_numbers


def _framework_metadata_matches(component: Path, inspection: PathInspection) -> bool:
    metadata = plistlib.loads(
        inspection.read_metadata(component / "Resources/Info.plist", 2 * 1024 * 1024)
    )
    return (
        isinstance(metadata, dict)
        and metadata.get("CFBundleIdentifier") == "com.google.Chrome.framework"
        and metadata.get("CFBundleShortVersionString") == component.name
    )


def _current_component(
    versions: Path, installed_version: str, inspection: PathInspection
) -> tuple[str | None, FileIdentity | None]:
    """保留指针文本供展示；只有同卷实体和 framework 元数据一致才返回身份。"""
    current = None
    try:
        with open_directory(versions, inspection.protection) as fd:
            current = os.readlink("Current", dir_fd=fd)
        if current != installed_version or _version_numbers(current) is None:
            return current, None
        # 数字版本只能是 Versions 下的直接子目录，不跟随 Current 或其它链接读取。
        parent = inspection.checked_path(versions)
        component = inspection.checked_path(versions / current)
        if (
            not stat.S_ISDIR(component.stat.st_mode)
            or component.stat.st_dev != parent.stat.st_dev
            or filesystem_id_retry(component.path) != filesystem_id_retry(versions)
            or not _framework_metadata_matches(component.path, inspection)
        ):
            return current, None
        return current, component.identity
    except (OSError, ValueError, TypeError, RecursionError, plistlib.InvalidFileException):
        return current, None


def scan_chrome_versions(
    roots: Iterable[str | os.PathLike[str]],
    protection: Predicate,
    *,
    checkpoint: Callable[[], None] = lambda: None,
    on_progress: Callable[[], None] = lambda: None,
    max_entries: int = 200_000,
) -> ScanResult:
    from .engine import Cancelled

    result = ScanResult()
    task = "应用旧版本组件"
    budget = WalkBudget(max_entries)
    try:
        for raw_root in roots:
            checkpoint()
            application = normalize_path(raw_root)
            if isinstance(
                protection, ProtectionGate
            ) and protection.knowledge_base_ignores(application):
                continue
            facts = FileFacts.from_path(application)
            if (
                facts.stat is None
                or protection.should_ignore(facts)
                or facts.is_dataless
            ):
                continue
            inspection = PathInspection(protection, checkpoint=checkpoint, timeout=10)
            try:
                installed = _read_app_metadata(application, inspection.checked_path)
                if installed is None:
                    result.issues.append(
                        ScanIssue(
                            "application_metadata_invalid",
                            "应用元数据无法读取，不能判断组件版本",
                            task,
                            application,
                        )
                    )
                    continue
                if installed.bundle_id != "com.google.Chrome":
                    continue
                versions = (
                    application
                    / "Contents/Frameworks/Google Chrome Framework.framework/Versions"
                )
                versions_facts = inspection.checked_path(versions)
                if not stat.S_ISDIR(versions_facts.stat.st_mode):
                    continue
                current, current_identity = _current_component(
                    versions, installed.version, inspection
                )
                current_known = current_identity is not None
                if not current_known:
                    result.issues.append(
                        ScanIssue(
                            "updater_metadata_invalid",
                            "Current、当前组件实体或 framework 元数据无法与已安装版本核对",
                            task,
                            versions / "Current",
                        )
                    )
                candidates = [
                    f
                    for f, _ in walk_resources(
                        versions,
                        protection,
                        result,
                        task,
                        budget=budget,
                        checkpoint=inspection.cooperate,
                        on_progress=on_progress,
                        descend=lambda f: False,
                    )
                    if stat.S_ISDIR(f.stat.st_mode)
                    and _version_numbers(f.path.name) is not None
                    and f.path.name != current
                ]
                for candidate in candidates:
                    start = len(result.issues)
                    state = "version_unknown"
                    try:
                        metadata_matches = _framework_metadata_matches(candidate.path, inspection)
                    except (
                        OSError,
                        ValueError,
                        TypeError,
                        RecursionError,
                        plistlib.InvalidFileException,
                    ):
                        metadata_matches = False
                    if current_known and metadata_matches:
                        comparison = _compare_versions(
                            candidate.path.name, installed.version
                        )
                        state = (
                            "older_version_residue"
                            if comparison is not None and comparison < 0
                            else "same_version_residue"
                            if comparison == 0
                            else "pending_update"
                            if comparison
                            else "version_unknown"
                        )
                    if state == "version_unknown":
                        result.issues.append(
                            ScanIssue(
                                "updater_metadata_invalid",
                                "组件版本证据不完整，不判为旧更新",
                                task,
                                candidate.path,
                            )
                        )
                    logical = allocated = 0
                    seen = set()
                    for child, _ in walk_resources(
                        candidate.path,
                        protection,
                        result,
                        task,
                        budget=budget,
                        checkpoint=inspection.cooperate,
                        on_progress=on_progress,
                    ):
                        if not stat.S_ISREG(child.stat.st_mode):
                            continue
                        identity = (child.stat.st_dev, child.stat.st_ino)
                        if identity not in seen:
                            seen.add(identity)
                            logical += child.logical_size
                            allocated += child.allocated_size
                    # 版本读取和计量不是事务，结束时复核当前组件、指针和应用元数据。
                    latest = _read_app_metadata(application, inspection.checked_path)
                    latest_current, latest_identity = _current_component(
                        versions, latest.version if latest is not None else installed.version,
                        inspection,
                    )
                    changed = (
                        (latest is not None and latest != installed)
                        or (latest_current is not None and latest_current != current)
                        or (current_identity is not None and latest_identity is not None
                            and latest_identity != current_identity)
                    )
                    if changed:
                        state = "version_unknown"
                        result.issues.append(
                            ScanIssue(
                                "path_changed",
                                "扫描期间应用版本、Current 或当前组件身份变化",
                                task,
                                application,
                            )
                        )
                    elif latest is None or latest_identity is None:
                        state = "version_unknown"
                        result.issues.append(
                            ScanIssue(
                                "updater_metadata_invalid",
                                "扫描结束时无法核实应用及当前组件元数据；不能确认版本状态",
                                task,
                                application,
                            )
                        )
                    complete = (
                        not any(i.blocking for i in result.issues[start:])
                        and current_known
                    )
                    note = (
                        f"Google Chrome：installed={installed.version}；component={candidate.path.name}；"
                        f"status={state}；应用组件占用单列，不与语言资源或其它诊断重复相加"
                    )
                    result.items.append(
                        Item(
                            candidate.path,
                            0,
                            task,
                            "critical",
                            note=note,
                            resource_kind="filesystem_subset",
                            diagnostic_kind="application_updates",
                            logical_size=logical,
                            resource_total_size=allocated,
                            total_count=len(seen),
                            identity=candidate.identity,
                            actionable=False,
                            preselected=False,
                            action_block_reason="签名应用内的旧组件仅诊断，不提供删除执行器",
                        )
                    )
                    result.diagnostics.append(
                        {
                            "kind": "application_updates",
                            "anchor_path": str(candidate.path),
                            "category": task,
                            "complete": complete,
                            "installed_version": installed.version,
                            "staged_version": candidate.path.name,
                            "updater_status": state,
                            "current_version": current,
                            "logical_bytes": logical,
                            "allocated_bytes": allocated,
                            "measurement_basis": "application_component_separate_total",
                        }
                    )
                    if budget.exhausted:
                        break
            except OSError as exc:
                result.issues.append(
                    ScanIssue(
                        "permission_denied"
                        if isinstance(exc, PermissionError)
                        else "updater_metadata_invalid",
                        str(exc),
                        task,
                        application,
                    )
                )
            if budget.exhausted:
                break
    except Cancelled:
        result.cancelled = True
    return result
