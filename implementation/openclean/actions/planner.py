"""Finding → CleanupPlan → 执行（AR-01 §7 / AR-04 §4-§6 / P0-correction SPEC）。

- ``resolve_plan`` 计算 ``can_execute`` 的**静态**合取项（用户授权 flag、trusted、supported、
  run 未过期、run 完整、finding 归属 run、strategy hash 匹配、非聚合根、inspect 期
  actionable、风险授权），产出可预览的 CleanupPlan。
- preview 模式下所有项 ``can_execute=false``，并携带 ``user_confirmation_required``。
- ``execute_plan`` 在执行前强制检查 ``mode != preview``、all-or-nothing 批次完整性和
  cross-run finding 绑定。
- Finding ID 不构成授权：``can_execute`` 只是客观可执行性，真正执行还需 CLI 层的 ``--yes``。
"""
from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from pathlib import Path

from ..cleanup import CleanupReport, execute_cleanup
from ..core.errors import FindingNotInRunError
from ..core.models import (
    CleanupOutcomeRecord,
    CleanupPlan,
    CleanupPlanItem,
    Finding,
    ResolvedTarget,
    Run,
)
from ..predicates import Predicate
from ..runtime.finding_projection import item_from_finding
from ..strategies.registry import StrategyRegistry


def resolve_plan(
    *,
    run: Run,
    findings: Sequence[Finding],
    selected_ids: Sequence[str],
    registry: StrategyRegistry,
    user_confirmed: bool,
    include_confirm: bool = False,
    include_critical: bool = False,
    now: float | None = None,
) -> CleanupPlan:
    now = time.time() if now is None else now
    by_id = {finding.finding_id: finding for finding in findings}
    plan_items: list[CleanupPlanItem] = []
    for finding_id in selected_ids:
        finding = by_id.get(finding_id)
        if finding is None:
            raise FindingNotInRunError(
                f"Finding {finding_id} 不属于 Run {run.run_id}"
            )
        # P0 BUG-005 fix：Finding 必须归属当前 Run（cross-run 拒绝）。
        if finding.run_id != run.run_id:
            raise FindingNotInRunError(
                f"Finding {finding_id} 属于 Run {finding.run_id}，"
                f"不属于 Run {run.run_id}"
            )
        strategy = registry.get(finding.strategy_id)
        block: list[str] = []
        # P0 BUG-004 fix：不完整 Run 不可执行。
        if not run.complete:
            block.append("run_incomplete")
        if run.expired(now):
            block.append("run_expired")
        if strategy.status != "trusted":
            block.append("strategy_not_trusted")
        if not strategy.action.supported:
            block.append("action_unsupported")
        expected_hash = run.strategy_pack_hashes.get(strategy.pack)
        if expected_hash is None or expected_hash != registry.pack_hash(strategy.pack):
            block.append("strategy_hash_mismatch")
        # AR-06 §4：聚合根（filesystem_subset）不得作为动作目标。
        if finding.target.kind != "filesystem":
            block.append("aggregate_root_not_actionable")
        if not finding.assessment.actionable:
            block.extend(finding.assessment.block_reasons or ["not_actionable"])
        risk = finding.assessment.action_risk
        if risk == "confirm" and not include_confirm:
            block.append("requires_include_confirm")
        if risk == "critical" and not include_critical:
            block.append("requires_include_critical")

        can_execute = not block
        # P0 BUG-002 fix：preview 模式下所有项 can_execute=false，
        # 并携带 user_confirmation_required。
        if not user_confirmed:
            can_execute = False
            reason_list = list(block)
            if "user_confirmation_required" not in reason_list:
                reason_list.append("user_confirmation_required")
            block = reason_list
        targets: tuple[ResolvedTarget, ...] = ()
        if finding.target.kind == "filesystem" and finding.target.display_path:
            targets = (
                ResolvedTarget(
                    display_path=finding.target.display_path,
                    identity=finding.target.identity,
                ),
            )
        plan_items.append(
            CleanupPlanItem(
                finding_id=finding_id,
                strategy_id=strategy.id,
                strategy_version=strategy.version,
                action_name=strategy.action.name,
                action_supported=strategy.action.supported,
                can_execute=can_execute,
                resolved_targets=targets,
                block_reasons=tuple(dict.fromkeys(block)),
            )
        )
    return CleanupPlan(
        mode="execute" if user_confirmed else "preview",
        run_id=run.run_id,
        executed=False,
        plan_items=tuple(plan_items),
    )


def execute_plan(
    plan: CleanupPlan,
    findings: Sequence[Finding],
    protection: Predicate,
    *,
    home: Path | None = None,
    uid: int | None = None,
    trash_resolver: Callable[[Path], Path] | None = None,
    process_runner: Callable[..., object] | None = None,
) -> tuple[CleanupReport, tuple[CleanupOutcomeRecord, ...]]:
    """执行 can_execute 的 Finding；复用 execute_cleanup 的全部安全复核。

    P0 安全不变量：
    - preview plan 永不执行（BUG-002）；
    - 任一 Finding blocked 时整批不开始（BUG-003 all-or-nothing）。
    """
    # P0 BUG-002 fix：preview plan 不可进入执行。
    if plan.mode == "preview":
        raise ValueError(
            "CleanupPlan mode=preview 不可执行；需要 mode=execute + --yes"
        )
    # P0 BUG-003 fix：all-or-nothing 批次检查。
    blocked_items = [item for item in plan.plan_items if not item.can_execute]
    if blocked_items:
        records = tuple(
            CleanupOutcomeRecord(
                finding_id=item.finding_id,
                status="blocked" if not item.can_execute else "not_run",
                bytes_affected=0,
                message=(
                    ", ".join(item.block_reasons) if item.block_reasons else "batch_blocked"
                ),
            )
            for item in plan.plan_items
        )
        report = CleanupReport(outcomes=[])
        return report, records
    by_id = {finding.finding_id: finding for finding in findings}
    executable = [
        by_id[item.finding_id] for item in plan.plan_items if item.can_execute
    ]
    items = [item_from_finding(finding) for finding in executable]
    report = execute_cleanup(
        items,
        protection,
        home=home,
        uid=uid,
        trash_resolver=trash_resolver,
        process_runner=process_runner,
    )
    records = tuple(
        CleanupOutcomeRecord(
            finding_id=finding.finding_id,
            status=outcome.status,
            bytes_affected=outcome.bytes_affected,
            destination=(
                str(outcome.destination)
                if outcome.destination is not None
                else None
            ),
            message=outcome.message,
        )
        # execute_cleanup 保持 outcomes 与输入 items 同序，可安全 zip。
        for finding, outcome in zip(executable, report.outcomes)
    )
    return report, records
