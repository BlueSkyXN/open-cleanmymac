"""Item ↔ Finding 双向投影（AR-01 §6 / AR-07 阶段 A 的关键接缝）。

迁移期 ``Item`` 仍是安全内核（``cleanup.execute_cleanup`` / detector）的执行通货，
``Finding`` 是 Agent 面向的运行时对象。本模块提供两个纯函数、无 I/O：

- ``finding_from_item``：把 ``Item`` 投影为 ``Finding``。语义字段进入
  ``target``/``measurement``/``assessment``/``recommendation``；**完整的、JSON 安全的 Item
  快照**进入 ``evidence.payload``，作为迁移期无损适配来源。
- ``item_from_finding``：从 ``evidence.payload`` 快照精确重建 ``Item``，重建结果必须能通过
  ``Item.__post_init__`` 全部不变量，并让 ``_audit_item`` 的硬阻断字段
  （``excluded_paths``/``cloud_file_count``/``is_cloud_file``/``requires_privilege``/``identity``/
  ``domain``/updater 三字段）原样生效。

R1（最高安全风险）：投影若丢这些字段会静默放宽安全闸，故此处对**全部** ``Item`` 字段做
无损快照，并以往返等价测试（tests/test_agent_projection.py）作为阶段 A 验收门。
"""
from __future__ import annotations

from dataclasses import fields
from pathlib import Path
from typing import Any

from ..core.models import (
    EVIDENCE_FILESYSTEM,
    Finding,
    FindingAssessment,
    FindingEvidence,
    FindingMeasurement,
    FindingTarget,
    Recommendation,
)
from ..models import FileIdentity, Item
from ..core.serialization import decode_dataclass

def _encode(value: Any) -> Any:
    """把 Item 字段值编码为 JSON 安全类型。"""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, FileIdentity):
        return {
            "device": value.device,
            "inode": value.inode,
            "owner": value.owner,
        }
    if isinstance(value, tuple):
        return [_encode(item) for item in value]
    return value


def item_to_payload(item: Item) -> dict[str, Any]:
    """完整、JSON 安全的 Item 字段快照（无损）。"""
    return {f.name: _encode(getattr(item, f.name)) for f in fields(item)}


def item_from_payload(payload: dict[str, Any]) -> Item:
    """从快照精确重建 Item；由 ``Item.__post_init__`` 兜底校验不变量。"""
    if not isinstance(payload, dict) or set(payload) != {f.name for f in fields(Item)}:
        raise ValueError("Item evidence must contain exactly the complete snapshot fields")
    return decode_dataclass(Item, payload, "evidence.payload")


def _classify(item: Item) -> str:
    if item.actionable:
        return "cleanup_candidate"
    if item.requires_privilege:
        return "protected"
    return "report_only"


def default_evidence_kind(item: Item) -> str:
    """只读诊断沿用 ``diagnostic_kind``；可执行/普通文件系统候选用非诊断 kind。"""
    return item.diagnostic_kind or EVIDENCE_FILESYSTEM


def finding_from_item(
    item: Item,
    *,
    finding_id: str,
    run_id: str,
    strategy_id: str,
    strategy_version: int,
    evidence_kind: str | None = None,
    do_not_do: tuple[str, ...] = (),
) -> Finding:
    """把 ``Item`` 投影为 ``Finding``（语义字段 + 无损 payload 快照）。"""
    kind = evidence_kind if evidence_kind is not None else default_evidence_kind(item)
    block_reasons = (
        (item.action_block_reason,)
        if (not item.actionable and item.action_block_reason)
        else ()
    )
    target = FindingTarget(
        kind=item.resource_kind,
        display_path=str(item.path) if item.path is not None else None,
        identifier=item.identifier,
        identity=item.identity,
    )
    measurement = FindingMeasurement(
        size=item.size,
        allocated_bytes=item.allocated_size,
        logical_bytes=item.logical_size,
        age_days=item.age_days,
        latest_mtime=item.latest_mtime,
    )
    assessment = FindingAssessment(
        classification=_classify(item),
        certainty="medium",
        action_risk=item.safety,
        actionable=item.actionable,
        block_reasons=block_reasons,
        requires_privilege=item.requires_privilege,
        is_cloud_file=item.is_cloud_file,
        requires_explicit_selection=item.requires_explicit_selection,
    )
    recommendation = Recommendation(summary=item.note, do_not_do=tuple(do_not_do))
    evidence = FindingEvidence(kind=kind, payload=item_to_payload(item))
    return Finding(
        finding_id=finding_id,
        run_id=run_id,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        target=target,
        measurement=measurement,
        assessment=assessment,
        evidence=evidence,
        recommendation=recommendation,
    )


def item_from_finding(finding: Finding) -> Item:
    """从 Finding 的 payload 快照精确重建 ``Item``，供展示、一致性检查和适配测试使用；执行时重新探测 Item。"""
    return item_from_payload(finding.evidence.payload)
