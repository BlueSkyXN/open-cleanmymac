"""Agent Runtime 领域模型不变量测试（AR-01 / AR-02）。"""
from __future__ import annotations

import unittest

from openclean.core.models import (
    Action,
    CleanupPlan,
    Detector,
    Finding,
    FindingAssessment,
    FindingEvidence,
    FindingMeasurement,
    FindingTarget,
    Provenance,
    Run,
    RunIssue,
    Strategy,
    StrategyPack,
)


def _strategy(**overrides) -> Strategy:
    base = {
        "id": "codex.marketplace.old-staging",
        "version": 1,
        "pack": "codex",
        "status": "active",
        "provenance": (Provenance(kind="personal_experience", observation_id="obs:1"),),
        "detector": Detector(name="codex_transient"),
        "action": Action(name="report_only", supported=False),
    }
    base.update(overrides)
    return Strategy(**base)


class StrategyInvariantTests(unittest.TestCase):
    def test_valid_active_strategy(self) -> None:
        self.assertTrue(_strategy().runtime_visible)

    def test_empty_provenance_must_be_draft(self) -> None:
        with self.assertRaisesRegex(ValueError, "draft"):
            _strategy(provenance=(), status="active")
        # draft 允许无 provenance
        self.assertFalse(_strategy(provenance=(), status="draft").runtime_visible)

    def test_trusted_requires_supported_action(self) -> None:
        with self.assertRaisesRegex(ValueError, "trusted"):
            _strategy(status="trusted", action=Action("move_to_trash", False))

    def test_non_trusted_cannot_support_action(self) -> None:
        with self.assertRaisesRegex(ValueError, "trusted"):
            _strategy(status="active", action=Action("move_to_trash", True))

    def test_id_must_match_pack_prefix(self) -> None:
        with self.assertRaisesRegex(ValueError, "pack 前缀"):
            _strategy(id="qoder.x", pack="codex")

    def test_version_must_be_positive_int(self) -> None:
        with self.assertRaises(ValueError):
            _strategy(version=0)

    def test_unknown_status_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _strategy(status="beta")


class StrategyPackInvariantTests(unittest.TestCase):
    def test_duplicate_id_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "重复"):
            StrategyPack(name="codex", strategies=(_strategy(), _strategy()))

    def test_pack_mismatch_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "不一致"):
            StrategyPack(name="codex", strategies=(_strategy(pack="qoder", id="qoder.x"),))

    def test_runtime_visible_filters_draft_and_deprecated(self) -> None:
        pack = StrategyPack(
            name="codex",
            strategies=(
                _strategy(id="codex.a", status="active"),
                _strategy(id="codex.b", status="draft", provenance=()),
                _strategy(id="codex.c", status="deprecated"),
                _strategy(
                    id="codex.d",
                    status="trusted",
                    action=Action("move_to_trash", True),
                ),
            ),
        )
        visible = {s.id for s in pack.runtime_visible()}
        self.assertEqual(visible, {"codex.a", "codex.d"})


class FindingInvariantTests(unittest.TestCase):
    def _finding(self, **kw) -> Finding:
        base = {
            "finding_id": "finding:abc",
            "run_id": "run:xyz",
            "strategy_id": "codex.x",
            "strategy_version": 1,
            "target": FindingTarget(kind="filesystem", display_path="/Users/u/a"),
            "measurement": FindingMeasurement(size=10),
            "assessment": FindingAssessment(
                classification="cleanup_candidate",
                certainty="high",
                action_risk="safe",
                actionable=True,
            ),
            "evidence": FindingEvidence(kind="filesystem", payload={}),
        }
        base.update(kw)
        return Finding(**base)

    def test_valid_actionable_filesystem_finding(self) -> None:
        self.assertTrue(self._finding().assessment.actionable)

    def test_readonly_evidence_cannot_be_actionable(self) -> None:
        with self.assertRaisesRegex(ValueError, "只读诊断"):
            self._finding(
                evidence=FindingEvidence(kind="retention", payload={}),
                assessment=FindingAssessment(
                    classification="report_only",
                    certainty="high",
                    action_risk="safe",
                    actionable=True,
                ),
            )

    def test_subset_target_requires_subset_evidence(self) -> None:
        with self.assertRaises(ValueError):
            self._finding(
                target=FindingTarget(kind="filesystem_subset", display_path="/Users/u/a"),
                evidence=FindingEvidence(kind="filesystem", payload={}),
                assessment=FindingAssessment(
                    classification="report_only",
                    certainty="high",
                    action_risk="safe",
                    actionable=False,
                ),
            )

    def test_bad_id_prefix_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._finding(finding_id="f:abc")

    def test_actionable_cannot_have_block_reasons(self) -> None:
        with self.assertRaisesRegex(ValueError, "block_reasons"):
            FindingAssessment(
                classification="cleanup_candidate",
                certainty="high",
                action_risk="safe",
                actionable=True,
                block_reasons=("x",),
            )

    def test_unknown_action_risk_rejected(self) -> None:
        with self.assertRaises(ValueError):
            FindingAssessment(
                classification="report_only",
                certainty="high",
                action_risk="low",  # 非 SAFETY_LEVELS
                actionable=False,
            )

    def test_filesystem_target_requires_display_path(self) -> None:
        with self.assertRaisesRegex(ValueError, "display_path"):
            FindingTarget(kind="filesystem")

    def test_non_filesystem_target_requires_identifier(self) -> None:
        with self.assertRaisesRegex(ValueError, "identifier"):
            FindingTarget(kind="docker")


class RunAndPlanTests(unittest.TestCase):
    def test_run_expiry(self) -> None:
        run = Run(
            run_id="run:a",
            created_at=100.0,
            expires_at=200.0,
            requested_target="codex",
            openclean_version="0.23.0",
            issues=(RunIssue(code="x", message="m", blocking=True),),
            complete=False,
        )
        self.assertFalse(run.expired(now=150.0))
        self.assertTrue(run.expired(now=200.0))

    def test_run_bad_prefix(self) -> None:
        with self.assertRaises(ValueError):
            Run(
                run_id="r:a",
                created_at=0.0,
                expires_at=1.0,
                requested_target="codex",
                openclean_version="0",
            )

    def test_preview_plan_cannot_be_executed(self) -> None:
        with self.assertRaisesRegex(ValueError, "executed"):
            CleanupPlan(mode="preview", run_id="run:a", executed=True)

    def test_unknown_mode_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CleanupPlan(mode="dry", run_id="run:a", executed=False)


if __name__ == "__main__":
    unittest.main()
