"""Finding plans with explicit consent, immutable target binding and live detection.

A blocked preflight prevents the whole batch from starting. Once an OS operation
has begun, later I/O failures are reported as partial progress, not rolled back.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from pathlib import Path

from ..cleanup import CleanupOutcome, CleanupReport, execute_cleanup
from .. import __version__
from ..core.errors import FindingNotInRunError, PlanError
from ..core.models import CleanupOutcomeRecord, CleanupPlan, CleanupPlanItem, Finding, ResolvedTarget, Run
from ..models import Item, normalize_path
from ..predicates import Predicate
from ..runtime.bundle_validation import validate_bundle, validate_finding
from ..runtime.inspect_service import Snapshots, _capture_snapshots, _scan_for_strategy, apply_strategy, validated_home
from ..strategies.registry import StrategyRegistry


def resolve_plan(
    *, run: Run, findings: Sequence[Finding], selected_ids: Sequence[str],
    registry: StrategyRegistry, user_confirmed: bool, include_confirm: bool = False,
    include_critical: bool = False, now: float | None = None,
) -> CleanupPlan:
    now = time.time() if now is None else now
    if type(user_confirmed) is not bool or type(include_confirm) is not bool or type(include_critical) is not bool:
        raise PlanError("Consent and risk options must be boolean")
    if not selected_ids or len(set(selected_ids)) != len(selected_ids):
        raise PlanError("Select one or more unique Finding IDs")
    by_id = {f.finding_id: f for f in findings}
    for finding_id in selected_ids:
        finding = by_id.get(finding_id)
        if finding is None or finding.run_id != run.run_id or finding_id not in run.finding_ids:
            raise FindingNotInRunError(f"Finding {finding_id} 不属于 Run {run.run_id}")
    try:
        validate_bundle(run, findings)
    except (ValueError, TypeError) as exc:
        raise PlanError(str(exc)) from exc
    paths = [by_id[f].target.display_path for f in selected_ids
             if by_id[f].target.kind == "filesystem"]
    if len(set(paths)) != len(paths):
        raise PlanError("Selected Findings resolve to duplicate filesystem targets")
    plan_items = []
    for finding_id in selected_ids:
        finding = by_id[finding_id]
        strategy = registry.get(finding.strategy_id)
        block = []
        if not run.complete:
            block.append("run_incomplete")
        if run.expired(now):
            block.append("run_expired")
        if run.openclean_version != __version__:
            block.append("runtime_version_mismatch")
        if not run.home:
            block.append("run_home_missing")
        if not registry.action_approved(strategy):
            block.append("strategy_not_trusted")
        if not strategy.action.supported or strategy.action.name != "move_to_trash":
            block.append("action_unsupported")
        if run.strategy_pack_hashes.get(strategy.pack) != registry.pack_hash(strategy.pack):
            block.append("strategy_hash_mismatch")
        if finding.strategy_version != strategy.version:
            block.append("strategy_version_mismatch")
        if strategy.conditions.require_structure_match:
            block.append("structure_match_unavailable")
        if finding.target.kind != "filesystem":
            block.append("aggregate_root_not_actionable")
        if not finding.assessment.actionable:
            block.extend(finding.assessment.block_reasons or ("not_actionable",))
        if finding.assessment.action_risk == "confirm" and not include_confirm:
            block.append("requires_include_confirm")
        if finding.assessment.action_risk == "critical" and not include_critical:
            block.append("requires_include_critical")
        if not user_confirmed:
            block.append("user_confirmation_required")
        targets = ()
        if finding.target.kind == "filesystem" and finding.target.display_path:
            targets = (ResolvedTarget(finding.target.display_path, finding.target.identity),)
        plan_items.append(CleanupPlanItem(
            finding_id=finding_id, strategy_id=finding.strategy_id,
            strategy_version=finding.strategy_version, action_name=strategy.action.name,
            action_supported=strategy.action.supported, can_execute=not block,
            resolved_targets=targets, block_reasons=tuple(dict.fromkeys(block)),
        ))
    return CleanupPlan(mode="execute" if user_confirmed else "preview", run_id=run.run_id,
                       executed=False, plan_items=tuple(plan_items))


def _blocked(
    plan: CleanupPlan, by_id: dict[str, Finding], reasons: dict[str, str],
) -> tuple[CleanupReport, tuple[CleanupOutcomeRecord, ...]]:
    outcomes, records = [], []
    for entry in plan.plan_items:
        status = "blocked" if entry.finding_id in reasons else "not_run"
        message = reasons.get(entry.finding_id, "batch_blocked")
        item = validate_finding(by_id[entry.finding_id])
        outcomes.append(CleanupOutcome(item=item, status=status, message=message))
        records.append(CleanupOutcomeRecord(entry.finding_id, status, message=message))
    return CleanupReport(outcomes=outcomes), tuple(records)


def execute_plan(
    plan: CleanupPlan, findings: Sequence[Finding], protection: Predicate, *,
    run: Run | None = None, registry: StrategyRegistry | None = None,
    user_confirmed: bool = False, include_confirm: bool = False,
    include_critical: bool = False, now: float | None = None,
    home: Path | None = None, uid: int | None = None,
    trash_resolver: Callable[[Path], Path] | None = None,
    process_runner: Callable[..., object] | None = None,
    snapshots: Snapshots | None = None,
) -> tuple[CleanupReport, tuple[CleanupOutcomeRecord, ...]]:
    """Never trust a submitted plan's booleans or serialized Item as authority.

    ``run``, ``registry`` and fresh ``user_confirmed`` are mandatory for execution.
    Snapshots and the Trash resolver are Python test seams, not CLI options.
    """
    if plan.mode != "execute":
        raise PlanError("preview plan 不可执行", code="preview_not_executable")
    if plan.executed:
        raise PlanError("An already-executed plan cannot be submitted again")
    if not plan.plan_items:
        raise PlanError("Empty execution plan")
    ids = [entry.finding_id for entry in plan.plan_items]
    if len(set(ids)) != len(ids):
        raise PlanError("Duplicate plan Finding")
    by_id = {f.finding_id: f for f in findings}
    if len(by_id) != len(findings):
        raise PlanError("Duplicate input Finding")
    try:
        for entry in plan.plan_items:
            finding = by_id.get(entry.finding_id)
            if finding is None or finding.run_id != plan.run_id:
                raise ValueError("Plan/Finding Run mismatch")
            validate_finding(finding)
            expected_targets = ((ResolvedTarget(finding.target.display_path, finding.target.identity),)
                                if finding.target.kind == "filesystem" else ())
            if (entry.strategy_id, entry.strategy_version, entry.resolved_targets) != (
                finding.strategy_id, finding.strategy_version, expected_targets,
            ):
                raise ValueError("Plan resolved target or strategy diverges from Finding")
    except (ValueError, TypeError) as exc:
        raise PlanError(str(exc)) from exc
    blocked = {e.finding_id: ", ".join(e.block_reasons) or "not_actionable"
               for e in plan.plan_items if not e.can_execute or e.block_reasons}
    if blocked:
        return _blocked(plan, by_id, blocked)
    if run is None or registry is None or user_confirmed is not True:
        raise PlanError("Execution requires Run, registry and explicit user consent")
    if run.run_id != plan.run_id:
        raise PlanError("Execution Run differs from plan")
    now = time.time() if now is None else now
    current_plan = resolve_plan(run=run, findings=findings, selected_ids=ids,
                                registry=registry, user_confirmed=user_confirmed,
                                include_confirm=include_confirm, include_critical=include_critical,
                                now=now)
    blocked = {e.finding_id: ", ".join(e.block_reasons) for e in current_plan.plan_items
               if not e.can_execute}
    if blocked:
        return _blocked(current_plan, by_id, blocked)
    if plan != current_plan:
        raise PlanError("Submitted plan differs from freshly resolved plan")
    selected_home = validated_home(home or run.home)
    if normalize_path(run.home) != selected_home:
        raise PlanError("Execution HOME differs from inspected HOME")
    issues = []
    processes, open_files = snapshots if snapshots is not None else _capture_snapshots(issues)
    live_by_strategy: dict[str, list[Item]] = {}
    for entry in plan.plan_items:
        if entry.strategy_id in live_by_strategy:
            continue
        strategy = registry.get(entry.strategy_id)
        scanned = _scan_for_strategy(strategy, protection, processes, open_files, selected_home, now=now)
        issues.extend(scanned.issues)
        live_by_strategy[entry.strategy_id] = [apply_strategy(item, strategy) for item in scanned.items]
    items = []
    for entry in plan.plan_items:
        target = entry.resolved_targets[0]
        matches = [item for item in live_by_strategy[entry.strategy_id]
                   if item.path is not None and str(item.path) == target.display_path]
        if any(issue.blocking for issue in issues):
            blocked[entry.finding_id] = "live_scan_incomplete"
        elif len(matches) != 1:
            blocked[entry.finding_id] = "live_target_not_found"
        else:
            live = matches[0]
            if live.identity != target.identity:
                blocked[entry.finding_id] = "live_identity_changed"
            elif not live.actionable:
                blocked[entry.finding_id] = live.action_block_reason or "live_target_blocked"
            elif live.resource_kind != "filesystem" or live.domain == "trash":
                blocked[entry.finding_id] = "action_resource_mismatch"
            elif live.open_handle_count != 0:
                blocked[entry.finding_id] = "live_open_handles_unknown_or_present"
            else:
                items.append(live)
    if blocked:
        return _blocked(plan, by_id, blocked)
    report = execute_cleanup(items, protection, home=selected_home, uid=uid,
                             trash_resolver=trash_resolver, process_runner=process_runner)
    if len(report.outcomes) != len(ids):
        raise PlanError("Executor returned an incomplete outcome sequence")
    records = tuple(CleanupOutcomeRecord(
        finding_id=finding_id, status=outcome.status, bytes_affected=outcome.bytes_affected,
        destination=str(outcome.destination) if outcome.destination is not None else None,
        message=outcome.message,
    ) for finding_id, outcome in zip(ids, report.outcomes))
    return report, records
