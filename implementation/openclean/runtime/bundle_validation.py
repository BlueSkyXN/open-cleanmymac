"""Bind the stored manifest, semantic Finding and legacy Item snapshot."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from ..core.models import Finding, Run
from ..core.serialization import decode_dataclass
from ..models import FILESYSTEM_RESOURCE_KINDS, Item, normalize_path
from .finding_projection import default_evidence_kind, item_from_finding


def validate_finding(finding: Finding) -> Item:
    # Dataclasses constructed by Python callers must obey the same types as JSON.
    decode_dataclass(Finding, asdict(finding), "finding")
    item = item_from_finding(finding)
    target, assessment, measure = finding.target, finding.assessment, finding.measurement
    if (target.kind, target.display_path, target.identifier, target.identity) != (
        item.resource_kind, str(item.path) if item.path is not None else None,
        item.identifier, item.identity,
    ):
        raise ValueError("Finding target and evidence snapshot diverge")
    if target.kind in FILESYSTEM_RESOURCE_KINDS:
        if item.identity is None:
            raise ValueError("Filesystem Finding has no identity")
        if str(normalize_path(target.display_path)) != target.display_path:
            raise ValueError("Finding path must be canonical and absolute")
        if any(type(v) is not int or v < 0 for v in (
            item.identity.device, item.identity.inode, item.identity.owner,
        )):
            raise ValueError("Invalid filesystem identity")
    if (assessment.action_risk, assessment.actionable, assessment.requires_privilege,
        assessment.is_cloud_file, assessment.requires_explicit_selection) != (
        item.safety, item.actionable, item.requires_privilege, item.is_cloud_file,
        item.requires_explicit_selection,
    ):
        raise ValueError("Finding assessment and evidence snapshot diverge")
    if (measure.size, measure.allocated_bytes, measure.logical_bytes,
        measure.age_days, measure.latest_mtime) != (
        item.size, item.allocated_size, item.logical_size, item.age_days, item.latest_mtime,
    ):
        raise ValueError("Finding measurement and evidence snapshot diverge")
    if finding.evidence.kind != default_evidence_kind(item):
        raise ValueError("Finding evidence kind and snapshot diverge")
    return item


def validate_bundle(run: Run, findings: Sequence[Finding]) -> None:
    decode_dataclass(Run, asdict(run), "run")
    ids = [f.finding_id for f in findings]
    finding_ids, manifest_ids = set(ids), set(run.finding_ids)
    if len(finding_ids) != len(ids) or len(manifest_ids) != len(run.finding_ids):
        raise ValueError("Duplicate Finding ID")
    if finding_ids != manifest_ids:
        raise ValueError("Run manifest does not match its Findings")
    for finding in findings:
        if finding.run_id != run.run_id:
            raise ValueError("Cross-run Finding")
        if run.strategy_versions.get(finding.strategy_id) != finding.strategy_version:
            raise ValueError("Finding version not present in Run manifest")
        pack = finding.strategy_id.split(".", 1)[0]
        if pack not in run.strategy_pack_hashes:
            raise ValueError("Finding pack not present in Run manifest")
        item = validate_finding(finding)
        if run.home and item.path is not None:
            if not item.path.is_relative_to(Path(run.home)):
                raise ValueError("Finding is outside the Run HOME")
    if run.home and str(normalize_path(run.home)) != run.home:
        raise ValueError("Run HOME must be canonical and absolute")
