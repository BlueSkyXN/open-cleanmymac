"""inspect 编排：按策略包驱动探测器，产出 Run + Finding（AR-03 §2 / AR-07 阶段 B）。

结构模板是 ``storage_diagnostics.scan_codex_storage_artifact_diagnostics``：**进程/句柄快照
只捕获一次**，共享给所有探测器（避免每个 detector 各调一次 ps+lsof，R4）。串行执行；
``task_graph`` 并发留待 P2 ``inspect all``。

探测器名称与子类型通过固定分支分派；未知 detector/subkind 走
``scanner_unavailable`` fail-closed（AGENTS.md 硬约束 6）。
"""
from __future__ import annotations

import platform
import stat
import time
from dataclasses import dataclass, replace
from pathlib import Path

from .. import __version__
from ..core.errors import AgentRuntimeError
from ..macos import TRUSTED_SCAN_ALIAS_ROOTS, symlink_component
from ..core.identifiers import new_finding_id, new_run_id
from ..core.models import Finding, Run, RunIssue, Strategy
from ..models import Item, ScanIssue, ScanResult, normalize_path
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
    findings: tuple[Finding, ...] = ()
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
            blocking=True,
        )
    )
    return result


def validated_home(home: str | Path | None) -> Path:
    value = normalize_path(home or Path.home())
    try:
        anchor = next((p for p in TRUSTED_SCAN_ALIAS_ROOTS if value.is_relative_to(p)), Path("/"))
        if symlink_component(value, anchor=anchor) is not None:
            raise ValueError("HOME has a symlink component")
        facts = value.lstat()
        if not stat.S_ISDIR(facts.st_mode) or stat.S_ISLNK(facts.st_mode):
            raise ValueError("HOME must be an existing, non-symlink directory")
    except (OSError, ValueError) as exc:
        raise AgentRuntimeError(str(exc), code="invalid_home") from exc
    return value


def _roots(strategy: Strategy, home: Path) -> list[str]:
    roots = []
    for raw in strategy.locator.roots:
        if raw == "~":
            root = home
        elif raw.startswith("~/"):
            root = home / raw[2:]
        elif raw.startswith("/"):
            root = Path(raw)
        else:
            raise ValueError("Locator must be absolute or HOME-relative")
        root = normalize_path(root)
        if not root.is_relative_to(home):
            raise ValueError("Locator is outside selected HOME")
        if symlink_component(root, anchor=home) is not None:
            raise ValueError("Locator has a symlink component")
        roots.append(str(root))
    return roots


def apply_strategy(item: Item, strategy: Strategy) -> Item:
    """Detector facts can only be made more restrictive by a strategy."""
    reasons = [item.action_block_reason] if item.action_block_reason else []
    if strategy.status != "trusted" or not strategy.action.supported:
        reasons.append("strategy_report_only")
    if strategy.action.name != "move_to_trash":
        reasons.append("action_unsupported")
    if strategy.conditions.require_structure_match:
        reasons.append("structure_match_unavailable")
    if strategy.assessment.classification != "cleanup_candidate":
        reasons.append("strategy_report_only")
    if strategy.conditions.require_no_open_handles and item.open_handle_count != 0:
        reasons.append("open_handles_unknown_or_present")
    if strategy.conditions.minimum_age_days is not None:
        if item.age_days is None or item.age_days < strategy.conditions.minimum_age_days:
            reasons.append("minimum_age_not_met")
    risks = {"safe": 0, "confirm": 1, "critical": 2}
    risk = max((item.safety, strategy.assessment.action_risk), key=risks.__getitem__)
    return replace(item, safety=risk, actionable=item.actionable and not reasons,
                   action_block_reason="; ".join(dict.fromkeys(reasons)),
                   preselected=False)



def _scan_for_strategy(
    strategy: Strategy,
    protection: Predicate,
    processes: ProcessSnapshot | None,
    open_files: OpenFileSnapshot | None,
    home: Path,
    *, now: float | None = None,
) -> ScanResult:
    name = strategy.detector.name
    params = strategy.detector.params
    subkind = params.get("subkind", "")
    try:
        roots = _roots(strategy, home)
    except ValueError as exc:
        result = ScanResult()
        result.issues.append(ScanIssue(code="invalid_locator", message=str(exc),
                                       task=strategy.id, blocking=True))
        return result
    snap = {"process_snapshot": processes, "open_files": open_files}

    if name == "codex_transient":
        if len(roots) != 1:
            return _unavailable(strategy, "需要且仅支持一个 locator root")
        root = normalize_path(roots[0])
        if subkind == "codex_marketplace_staging":
            return scan_codex_marketplace_staging(root, protection, anchor=home, now=now, **snap)
        if subkind == "codex_marketplace_staging_targets":
            return enumerate_codex_marketplace_staging_targets(
                root,
                protection,
                anchor=home,
                now=now,
                minimum_age_days=strategy.conditions.minimum_age_days or 0,
                name_glob=params.get("name_glob", "marketplace-upgrade-*"),
                **snap,
            )
        if subkind == "codex_git_skeleton":
            return scan_codex_git_skeletons(root, protection, anchor=home, now=now, **snap)
        return _unavailable(strategy, f"未知 codex_transient subkind：{subkind!r}")

    if name == "crashpad":
        if len(roots) != 1:
            return _unavailable(strategy, "需要且仅支持一个 locator root")
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
            partition_rules, partition_issues = discover_codex_log_partition_rules(home=home)
            rules.extend(partition_rules)
            result.issues.extend(partition_issues)
        if not rules:
            return _unavailable(strategy, "缺少 locator.roots")
        scanned = scan_retention_rules(rules, protection, now=now, **snap)
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
    home_path = validated_home(home)
    if target == "all":
        packs = registry.packs
    else:
        packs = (registry.get_pack(target),)  # 未加载 → PackNotFoundError

    issues: list[ScanIssue] = []
    processes, open_files = (
        snapshots if snapshots is not None else _capture_snapshots(issues)
    )

    run_id = new_run_id()
    findings: list[Finding] = []
    pack_hashes: dict[str, str] = {}
    for pack in packs:
        pack_hashes[pack.name] = pack_hash(pack)
        for strategy in pack.runtime_visible():
            scanned = _scan_for_strategy(
                strategy, protection, processes, open_files, home_path, now=now
            )
            issues.extend(scanned.issues)
            for detected in scanned.items:
                item = apply_strategy(detected, strategy)
                finding = finding_from_item(
                    item, finding_id=new_finding_id(), run_id=run_id,
                    strategy_id=strategy.id, strategy_version=strategy.version,
                    do_not_do=strategy.recommendation.do_not_do,
                )
                finding = replace(
                    finding,
                    assessment=replace(finding.assessment,
                        certainty=strategy.assessment.certainty,
                        classification=(strategy.assessment.classification
                            if item.actionable else
                            "protected" if item.requires_privilege else "report_only")),
                    recommendation=replace(finding.recommendation,
                        summary=strategy.recommendation.summary or item.note),
                )
                findings.append(finding)

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
        strategy_versions={s.id: s.version for pack in packs for s in pack.runtime_visible()},
        home=str(home_path),
    )
    run_store.save(run, findings)
    return InspectResult(run=run, findings=tuple(findings), issues=tuple(issues))
