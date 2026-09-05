"""inspect 编排：按策略包驱动探测器，产出 Run + Finding（AR-03 §2 / AR-07 阶段 B）。

结构模板是 ``storage_diagnostics.scan_codex_storage_artifact_diagnostics``：**进程/句柄快照
只捕获一次**，共享给所有探测器（避免每个 detector 各调一次 ps+lsof，R4）。串行执行；
``task_graph`` 并发留待 P2 ``inspect all``。

探测器分派用白名单 dict（替代 ``engine.py`` 的 if/elif 链）；未知 detector/subkind 走
``scanner_unavailable`` fail-closed（AGENTS.md 硬约束 6）。
"""
from __future__ import annotations

import platform
import time
from dataclasses import dataclass
from pathlib import Path

from .. import __version__
from ..core.identifiers import new_finding_id, new_run_id
from ..core.models import Run, RunIssue, Strategy
from ..models import ScanIssue, ScanResult, normalize_path
from ..predicates import Predicate
from ..processes import (
    OpenFileDetectionError,
    OpenFileSnapshot,
    ProcessDetectionError,
    ProcessSnapshot,
    capture_open_file_snapshot,
    capture_process_snapshot,
)
from ..storage_diagnostics import (
    _CODEX_PROCESS_MARKERS,
    RetentionRule,
    SQLiteRule,
    discover_codex_log_partition_rules,
    enumerate_codex_marketplace_staging_targets,
    scan_codex_git_skeletons,
    scan_codex_marketplace_staging,
    scan_crashpad_orphan_sidecars,
    scan_retention_rules,
    scan_sqlite_rules,
)
from ..strategies.registry import pack_hash
from .finding_projection import finding_from_item
from .run_store import RunStore, new_run_expiry

Snapshots = tuple[ProcessSnapshot | None, OpenFileSnapshot | None]


@dataclass(frozen=True)
class InspectResult:
    run: Run
    findings: tuple = ()
    issues: tuple[ScanIssue, ...] = ()


def _capture_snapshots(issues: list[ScanIssue]) -> Snapshots:
    try:
        processes: ProcessSnapshot | None = capture_process_snapshot()
    except ProcessDetectionError as exc:
        processes = None
        issues.append(
            ScanIssue(
                code="process_detection_failed",
                message=str(exc),
                task="inspect",
                blocking=False,
            )
        )
    try:
        open_files: OpenFileSnapshot | None = capture_open_file_snapshot()
    except OpenFileDetectionError as exc:
        open_files = None
        issues.append(
            ScanIssue(
                code="open_file_detection_failed",
                message=str(exc),
                task="inspect",
                blocking=False,
            )
        )
    return processes, open_files


def _unavailable(strategy: Strategy, reason: str) -> ScanResult:
    result = ScanResult()
    result.issues.append(
        ScanIssue(
            code="scanner_unavailable",
            message=f"策略 {strategy.id} 的探测器不可用：{reason}",
            task="inspect",
            blocking=False,
        )
    )
    return result


def _roots(strategy: Strategy) -> list[str]:
    return list(strategy.locator.roots)


def _scan_for_strategy(
    strategy: Strategy,
    protection: Predicate,
    processes: ProcessSnapshot | None,
    open_files: OpenFileSnapshot | None,
    home: Path,
) -> ScanResult:
    name = strategy.detector.name
    params = strategy.detector.params
    subkind = params.get("subkind", "")
    roots = _roots(strategy)
    snap = {"process_snapshot": processes, "open_files": open_files}

    if name == "codex_transient":
        if not roots:
            return _unavailable(strategy, "缺少 locator.roots")
        root = normalize_path(roots[0])
        if subkind == "codex_marketplace_staging":
            return scan_codex_marketplace_staging(root, protection, anchor=home, **snap)
        if subkind == "codex_marketplace_staging_targets":
            return enumerate_codex_marketplace_staging_targets(
                root,
                protection,
                anchor=home,
                minimum_age_days=strategy.conditions.minimum_age_days or 0,
                name_glob=params.get("name_glob", "marketplace-upgrade-*"),
                **snap,
            )
        if subkind == "codex_git_skeleton":
            return scan_codex_git_skeletons(root, protection, anchor=home, **snap)
        return _unavailable(strategy, f"未知 codex_transient subkind：{subkind!r}")

    if name == "crashpad":
        if not roots:
            return _unavailable(strategy, "缺少 locator.roots")
        return scan_crashpad_orphan_sidecars(
            normalize_path(roots[0]), protection, anchor=home, **snap
        )

    if name == "sqlite_freelist":
        category = params.get("category", strategy.id)
        rules = [
            SQLiteRule(category, root, _CODEX_PROCESS_MARKERS) for root in roots
        ]
        if not rules:
            return _unavailable(strategy, "缺少 locator.roots")
        return scan_sqlite_rules(rules, protection, **snap)

    if name == "retention":
        category = params.get("category", strategy.id)
        rules = [
            RetentionRule(category, root, _CODEX_PROCESS_MARKERS) for root in roots
        ]
        result = ScanResult()
        if params.get("include_partitions"):
            partition_rules, partition_issues = discover_codex_log_partition_rules()
            rules.extend(partition_rules)
            result.issues.extend(partition_issues)
        if not rules:
            return _unavailable(strategy, "缺少 locator.roots")
        scanned = scan_retention_rules(rules, protection, **snap)
        result.items.extend(scanned.items)
        result.issues.extend(scanned.issues)
        return result

    return _unavailable(strategy, f"未接线的 detector：{name!r}")


def inspect_target(
    target: str,
    protection: Predicate,
    *,
    registry,
    run_store: RunStore,
    now: float | None = None,
    home: str | Path | None = None,
    snapshots: Snapshots | None = None,
    protect_config_hash: str = "",
    openclean_version: str | None = None,
    macos_version: str | None = None,
) -> InspectResult:
    """运行一次 inspect：加载 pack → 逐策略探测 → 投影 Finding → 固化 Run。"""
    now = time.time() if now is None else now
    home_path = normalize_path(home or Path.home())
    if target == "all":
        packs = registry.packs
    else:
        packs = (registry.get_pack(target),)  # 未加载 → PackNotFoundError

    issues: list[ScanIssue] = []
    processes, open_files = (
        snapshots if snapshots is not None else _capture_snapshots(issues)
    )

    run_id = new_run_id()
    findings: list = []
    pack_hashes: dict[str, str] = {}
    for pack in packs:
        pack_hashes[pack.name] = pack_hash(pack)
        for strategy in pack.runtime_visible():
            scanned = _scan_for_strategy(
                strategy, protection, processes, open_files, home_path
            )
            issues.extend(scanned.issues)
            for item in scanned.items:
                findings.append(
                    finding_from_item(
                        item,
                        finding_id=new_finding_id(),
                        run_id=run_id,
                        strategy_id=strategy.id,
                        strategy_version=strategy.version,
                        do_not_do=strategy.recommendation.do_not_do,
                    )
                )

    run = Run(
        run_id=run_id,
        created_at=now,
        expires_at=new_run_expiry(now),
        requested_target=target,
        openclean_version=openclean_version or __version__,
        macos_version=macos_version or platform.mac_ver()[0],
        strategy_pack_hashes=pack_hashes,
        protect_config_hash=protect_config_hash,
        complete=not any(issue.blocking for issue in issues),
        issues=tuple(
            RunIssue(code=i.code, message=i.message, blocking=i.blocking)
            for i in issues
        ),
        finding_ids=tuple(f.finding_id for f in findings),
    )
    run_store.save(run, findings)
    return InspectResult(run=run, findings=tuple(findings), issues=tuple(issues))
