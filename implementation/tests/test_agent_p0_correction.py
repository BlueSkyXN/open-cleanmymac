"""P0 regression tests: each negative starts with a nonempty, otherwise executable fixture.

Synthetic approvals here never promote the shipped Codex pack. No conditional
assertions or name/age-only fixtures are treated as production evidence.
"""
from __future__ import annotations

import os
import time
from dataclasses import replace
from unittest import mock

from agent_fixtures import AgentFixture, approved_registry
from openclean.actions.planner import execute_plan
from openclean.core.errors import FindingNotInRunError, PlanError, RunStoreError
from openclean.core.models import RunIssue, StrategyPack
from openclean.runtime.finding_projection import finding_from_item, item_from_finding
from openclean.runtime.run_store import RunStore
from openclean.strategies.registry import StrategyRegistry


class P0CorrectionTests(AgentFixture):
    def test_bug001_finding_without_run_never_scans(self):
        with mock.patch("openclean.cli.scan_domains") as scan:
            code, payload = self.cli(["clean", "--finding", "finding:fake", "--yes", "--json"])
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"]["code"], "agent_selection_requires_run")
        self.assertFalse(payload["executed"])
        scan.assert_not_called()

    def test_bug001_mixed_modes_never_load_or_scan(self):
        for extra in (["system"], ["--select", str(self.target)], ["--all"], ["--force"],
                      ["--workers", "1"], ["--workers=1"]):
            with self.subTest(extra=extra), mock.patch("openclean.cli.scan_domains") as scan, \
                 mock.patch("openclean.cli._cmd_clean_finding") as clean:
                code, payload = self.cli(["clean", "--run", "run:fake", "--finding", "finding:fake",
                                          "--yes", "--json", *extra])
                self.assertEqual(code, 2)
                self.assertFalse(payload["executed"])
                scan.assert_not_called()
                clean.assert_not_called()

    def test_bug002_direct_preview_is_rejected(self):
        result = self.inspect()
        self.assertTrue(self.plan().plan_items[0].can_execute)
        plan = self.plan(user_confirmed=False)
        self.assertFalse(plan.plan_items[0].can_execute)
        self.assertIn("user_confirmation_required", plan.plan_items[0].block_reasons)
        with mock.patch("openclean.actions.planner.execute_cleanup") as action:
            with self.assertRaises(PlanError):
                self.execute(plan, result)
            action.assert_not_called()
        self.assertTrue(self.target.exists())

    def test_bug003_mixed_batch_is_not_started(self):
        self.make_target("marketplace-upgrade-recent", days=0)
        self.inspect()
        plan = self.plan()
        self.assertEqual(sorted(i.can_execute for i in plan.plan_items), [False, True])
        with mock.patch("openclean.actions.planner.execute_cleanup") as action:
            report, records = self.execute(plan)
            action.assert_not_called()
        self.assertFalse(report.complete)
        self.assertEqual(sorted(r.status for r in records), ["blocked", "not_run"])
        self.assertTrue(all(r.bytes_affected == 0 for r in records))
        self.assertTrue(self.target.exists())

    def test_bug004_incomplete_run_blocks(self):
        result = self.inspect()
        self.assertTrue(self.plan().plan_items[0].can_execute)
        run = replace(result.run, complete=False, issues=(RunIssue("missing", "partial", True),))
        plan = self.plan(run=run)
        self.assertFalse(plan.plan_items[0].can_execute)
        self.assertIn("run_incomplete", plan.plan_items[0].block_reasons)

    def test_bug005_cross_run_finding_is_rejected(self):
        result = self.inspect()
        bad = replace(result.findings[0], run_id="run:another")
        with self.assertRaises(FindingNotInRunError):
            self.plan(findings=(bad,))
        with self.assertRaises(RunStoreError):
            self.store.save(replace(result.run, run_id="run:new"), (bad,))

    def test_bug005_unlisted_finding_is_rejected(self):
        result = self.inspect()
        with self.assertRaises(FindingNotInRunError):
            self.plan(run=replace(result.run, finding_ids=()))

    def test_bug006_actual_target_payload_divergence_is_rejected(self):
        result = self.inspect()
        good_plan = self.plan()
        self.assertTrue(good_plan.plan_items[0].can_execute)
        finding = result.findings[0]
        payload = dict(finding.evidence.payload)
        payload["path"] = str(self.home / "Documents/valuable-project")
        bad = replace(finding, evidence=replace(finding.evidence, payload=payload))
        with mock.patch("openclean.actions.planner.execute_cleanup") as action:
            with self.assertRaises(PlanError):
                self.plan(findings=(bad,))
            with self.assertRaises(PlanError):
                execute_plan(good_plan, (bad,), self.protection)
            action.assert_not_called()
        self.assertTrue(self.target.exists())

    def test_bug006_plan_target_cannot_be_replaced(self):
        self.inspect()
        good = self.plan()
        target = replace(good.plan_items[0].resolved_targets[0], display_path=str(self.home / "valuable"))
        bad = replace(good, plan_items=(replace(good.plan_items[0], resolved_targets=(target,)),))
        with self.assertRaises(PlanError):
            self.execute(bad)

    def test_finding_version_is_not_silently_replaced(self):
        result = self.inspect()
        newer = replace(self.strategy, version=2)
        registry = approved_registry((StrategyPack("codex", (newer,)),))
        plan = self.plan(registry=registry)
        self.assertEqual(plan.plan_items[0].strategy_version, result.findings[0].strategy_version)
        self.assertIn("strategy_version_mismatch", plan.plan_items[0].block_reasons)

    def test_bug007_unapproved_pack_is_report_only(self):
        registry = StrategyRegistry((self.pack,))
        strategy = registry.get(self.strategy.id)
        self.assertEqual(strategy.status, "active")
        self.assertFalse(strategy.action.supported)
        result = self.inspect(registry=registry)
        self.assertEqual(len(result.findings), 1)
        self.assertFalse(result.findings[0].assessment.actionable)

    def test_bug008_old_arbitrary_contents_remain_report_only(self):
        (self.target / "important.json").write_text('{"precious": true}')
        old = time.time() - 9 * 86400
        for path in (self.target / "blob", self.target / "important.json", self.target):
            os.utime(path, (old, old))
        registry = StrategyRegistry.load(("codex",))
        result = self.inspect(registry=registry)
        targets = [f for f in result.findings if f.strategy_id == "codex.marketplace.old-staging"]
        self.assertEqual(len(targets), 1)
        self.assertFalse(targets[0].assessment.actionable)
        self.assertIn("structure_match_unavailable", targets[0].assessment.block_reasons[0])
        self.assertEqual(targets[0].assessment.classification, "report_only")

    def test_structure_condition_cannot_be_ignored_even_by_approved_fixture(self):
        strategy = replace(self.strategy, conditions=replace(self.strategy.conditions, require_structure_match=True))
        registry = approved_registry((StrategyPack("codex", (strategy,)),))
        result = self.inspect(registry=registry)
        self.assertEqual(len(result.findings), 1)
        self.assertFalse(result.findings[0].assessment.actionable)

    def test_home_override_works_without_changing_environment(self):
        decoy = self.root / "decoy-home"
        decoy.mkdir()
        with mock.patch.dict(os.environ, {"HOME": str(decoy)}):
            result = self.inspect(home=self.home)
        self.assertEqual(len(result.findings), 1)
        self.assertEqual(result.findings[0].target.display_path, str(self.target))
        self.assertEqual(result.run.home, str(self.home))
        self.assertEqual(list(decoy.iterdir()), [])

    def test_private_symlink_directory_is_rejected_without_chmod(self):
        actual = self.root / "private-runs"
        actual.mkdir(mode=0o700)
        link = self.root / "linked-runs"
        link.symlink_to(actual, target_is_directory=True)
        store = RunStore(link)
        result = self.inspect()
        with self.assertRaises(RunStoreError):
            store.save(result.run, result.findings)
        self.assertEqual(list(actual.iterdir()), [])

    def test_direct_execute_requires_fresh_consent_and_context(self):
        result = self.inspect()
        plan = self.plan()
        self.assertTrue(plan.plan_items[0].can_execute)
        with self.assertRaises(PlanError):
            execute_plan(plan, result.findings, self.protection)
        with self.assertRaises(PlanError):
            self.execute(plan, user_confirmed=False)

    def test_domain_tamper_is_not_used_to_dispatch_empty_trash(self):
        result = self.inspect()
        finding = result.findings[0]
        item = replace(item_from_finding(finding), domain="trash")
        bad = finding_from_item(item, finding_id=finding.finding_id, run_id=finding.run_id,
                                strategy_id=finding.strategy_id, strategy_version=finding.strategy_version)
        changed = replace(result, findings=(bad,))
        plan = self.plan(changed)
        with mock.patch("openclean.actions.planner.execute_cleanup") as action:
            # Check actual live Item passed to the shared executor, not serialized metadata.
            from openclean.cleanup import CleanupOutcome, CleanupReport
            action.side_effect = lambda items, *a, **k: CleanupReport([
                CleanupOutcome(item=i, status="moved_to_trash") for i in items])
            self.execute(plan, changed)
        self.assertEqual(action.call_count, 1)
        self.assertEqual(action.call_args.args[0][0].domain, "ai")
