"""resolve_plan 的 can_execute 合取矩阵测试（AR-04 §4 / AR-06 §3）。"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from openclean.actions.planner import resolve_plan
from openclean.core.models import (
    Action,
    Locator,
    Provenance,
    Strategy,
    StrategyAssessment,
    StrategyPack,
)
from openclean.knowledge_base import KnowledgeBase
from openclean.predicates import ProtectionGate
from openclean.processes import OpenFileSnapshot, ProcessSnapshot
from openclean.runtime.inspect_service import inspect_target
from openclean.runtime.run_store import RunStore
from openclean.strategies.registry import StrategyRegistry

_EMPTY = (ProcessSnapshot(()), OpenFileSnapshot(()))


def openclean_detector():
    from openclean.core.models import Detector
    return Detector(
        name="codex_transient",
        params={"subkind": "codex_marketplace_staging_targets", "name_glob": "marketplace-upgrade-*"},
    )


class ResolvePlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.home = root / "home"
        self.home.mkdir()
        env = mock.patch.dict(
            os.environ,
            {"HOME": str(self.home), "XDG_STATE_HOME": str(root / "state")},
        )
        env.start()
        self.addCleanup(env.stop)

        staging = self.home / ".codex/.tmp/marketplaces/.staging"
        old = staging / "marketplace-upgrade-old"
        old.mkdir(parents=True)
        (old / "blob.bin").write_bytes(b"x" * 4096)
        when = time.time() - 8 * 86400
        os.utime(old / "blob.bin", (when, when))
        os.utime(old, (when, when))
        self.old_target = old
        new = staging / "marketplace-upgrade-new"
        new.mkdir(parents=True)
        (new / "b.bin").write_bytes(b"y" * 1024)

        self.protection = ProtectionGate(KnowledgeBase.empty())
        self.registry = StrategyRegistry.load(names=("codex",))
        self.store = RunStore(directory=root / "state" / "openclean" / "runs")
        self.result = inspect_target(
            "codex",
            self.protection,
            registry=self.registry,
            run_store=self.store,
            home=self.home,
            snapshots=_EMPTY,
        )
        self.run = self.result.run
        self.findings = list(self.result.findings)

    def _finding(self, strategy_id: str, *, actionable: bool | None = None):
        for finding in self.findings:
            if finding.strategy_id != strategy_id:
                continue
            if actionable is None or finding.assessment.actionable == actionable:
                return finding
        raise AssertionError(f"no finding for {strategy_id} actionable={actionable}")

    def _resolve(self, finding, **kw):
        params = {
            "run": self.run,
            "findings": self.findings,
            "selected_ids": [finding.finding_id],
            "registry": self.registry,
            "user_confirmed": kw.pop("user_confirmed", True),
            "include_confirm": kw.pop("include_confirm", True),
            "include_critical": kw.pop("include_critical", False),
        }
        params.update(kw)
        return resolve_plan(**params).plan_items[0]

    def test_trusted_actionable_confirm_authorized_can_execute(self) -> None:
        from openclean.core.models import Conditions
        trusted_strategy = Strategy(
            id="codex.test.old-staging",
            version=1,
            pack="codex",
            status="trusted",
            provenance=(Provenance(kind="personal_experience", observation_id="obs:test"),),
            locator=Locator(roots=("~/.codex/.tmp/marketplaces/.staging",)),
            detector=openclean_detector(),
            conditions=Conditions(minimum_age_days=7),
            action=Action(name="move_to_trash", supported=True),
            assessment=StrategyAssessment(
                classification="cleanup_candidate", certainty="high", action_risk="confirm"
            ),
        )
        trusted_pack = StrategyPack(name="codex", strategies=(trusted_strategy,))
        registry = StrategyRegistry((trusted_pack,))
        result = inspect_target(
            "codex",
            self.protection,
            registry=registry,
            run_store=self.store,
            home=self.home,
            snapshots=_EMPTY,
        )
        finding = next(
            f for f in result.findings
            if f.strategy_id == "codex.test.old-staging"
            and f.assessment.actionable
        )
        plan = resolve_plan(
            run=result.run,
            findings=result.findings,
            selected_ids=[finding.finding_id],
            registry=registry,
            user_confirmed=True,
            include_confirm=True,
        )
        item = plan.plan_items[0]
        self.assertTrue(item.can_execute)
        self.assertEqual(item.block_reasons, ())
        self.assertEqual(item.action_name, "move_to_trash")
        self.assertTrue(item.action_supported)
        self.assertEqual(len(item.resolved_targets), 1)
        self.assertEqual(item.resolved_targets[0].display_path, str(self.old_target))

    def test_confirm_risk_requires_include_confirm(self) -> None:
        item = self._resolve(
            self._finding("codex.marketplace.old-staging", actionable=True),
            include_confirm=False,
        )
        self.assertFalse(item.can_execute)
        self.assertIn("requires_include_confirm", item.block_reasons)

    def test_active_summary_not_trusted_and_aggregate_root(self) -> None:
        item = self._resolve(self._finding("codex.marketplace.staging-summary"))
        self.assertFalse(item.can_execute)
        self.assertIn("strategy_not_trusted", item.block_reasons)
        self.assertIn("aggregate_root_not_actionable", item.block_reasons)
        self.assertEqual(item.resolved_targets, ())

    def test_young_target_not_actionable(self) -> None:
        item = self._resolve(
            self._finding("codex.marketplace.old-staging", actionable=False)
        )
        self.assertFalse(item.can_execute)
        self.assertTrue(item.block_reasons)

    def test_strategy_hash_mismatch_blocks(self) -> None:
        tampered = replace(
            self.run, strategy_pack_hashes={"codex": "sha256:deadbeef"}
        )
        plan = resolve_plan(
            run=tampered,
            findings=self.findings,
            selected_ids=[
                self._finding("codex.marketplace.old-staging", actionable=True).finding_id
            ],
            registry=self.registry,
            user_confirmed=True,
            include_confirm=True,
        )
        self.assertIn("strategy_hash_mismatch", plan.plan_items[0].block_reasons)

    def test_expired_run_blocks(self) -> None:
        expired = replace(self.run, expires_at=time.time() - 1)
        plan = resolve_plan(
            run=expired,
            findings=self.findings,
            selected_ids=[
                self._finding("codex.marketplace.old-staging", actionable=True).finding_id
            ],
            registry=self.registry,
            user_confirmed=True,
            include_confirm=True,
        )
        self.assertIn("run_expired", plan.plan_items[0].block_reasons)

    def test_preview_mode_when_not_confirmed(self) -> None:
        plan = resolve_plan(
            run=self.run,
            findings=self.findings,
            selected_ids=[
                self._finding("codex.marketplace.old-staging", actionable=True).finding_id
            ],
            registry=self.registry,
            user_confirmed=False,
            include_confirm=True,
        )
        self.assertEqual(plan.mode, "preview")
        self.assertFalse(plan.executed)


if __name__ == "__main__":
    unittest.main()
