"""Independent negatives with a demonstrably executable synthetic baseline."""
from __future__ import annotations

import os
import time
from dataclasses import replace
from unittest import mock

from agent_fixtures import AgentFixture, EMPTY, approved_registry
from openclean.core.errors import PlanError
from openclean.core.models import StrategyPack
from openclean.engine import IgnoreRules
from openclean.processes import OpenFileSnapshot, ProcessSnapshot


class ReviewExecutionTests(AgentFixture):
    def setUp(self):
        super().setUp()
        self.result = self.inspect()
        self.executable = self.plan()
        self.assertTrue(self.executable.plan_items[0].can_execute)

    def assert_live_blocked(self, **kwargs):
        with mock.patch("openclean.actions.planner.execute_cleanup") as action:
            report, records = self.execute(self.executable, **kwargs)
        action.assert_not_called()
        self.assertFalse(report.complete)
        self.assertEqual([r.status for r in records], ["blocked"])
        self.assertTrue(self.target.exists())
        return records[0].message

    def test_expiry_between_plan_and_execute(self):
        self.assertIn("run_expired", self.assert_live_blocked(now=self.result.run.expires_at + 1))

    def test_new_open_handle_after_inspect(self):
        self.assert_live_blocked(snapshots=(EMPTY[0], OpenFileSnapshot((str(self.target / "blob"),))))

    def test_failed_live_handle_capture(self):
        self.assert_live_blocked(snapshots=(EMPTY[0], None))

    def test_new_codex_process_after_inspect(self):
        self.assert_live_blocked(snapshots=(ProcessSnapshot(("/Applications/Codex.app/Contents/MacOS/Codex",)), EMPTY[1]))

    def test_new_root_mtime_after_inspect(self):
        now = time.time()
        os.utime(self.target, (now, now))
        self.assert_live_blocked()

    def test_new_child_after_inspect(self):
        (self.target / "fresh-content").write_bytes(b"new work")
        self.assert_live_blocked()

    def test_target_identity_replaced_between_inspect_and_execute(self):
        self.target.rename(self.target.with_name("retained-original"))
        self.make_target(self.target.name)
        self.assertIn("identity", self.assert_live_blocked())

    def test_new_ignore_rule_after_inspect(self):
        protection = IgnoreRules((str(self.target),))
        with mock.patch("openclean.actions.planner.execute_cleanup") as action:
            from openclean.actions.planner import execute_plan
            report, records = execute_plan(self.executable, self.result.findings, protection,
                run=self.result.run, registry=self.registry, user_confirmed=True,
                include_confirm=True, home=self.home, snapshots=EMPTY)
        action.assert_not_called()
        self.assertFalse(report.complete)
        self.assertEqual(records[0].status, "blocked")

    def test_tampered_action_name_not_authority(self):
        bad = replace(self.executable.plan_items[0], action_name="empty_trash")
        with self.assertRaises(PlanError):
            self.execute(replace(self.executable, plan_items=(bad,)))
        self.assertTrue(self.target.exists())

    def test_duplicate_plan_entries_rejected(self):
        with self.assertRaises(PlanError):
            self.execute(replace(self.executable, plan_items=self.executable.plan_items * 2))

    def test_already_executed_and_empty_plans_rejected(self):
        for plan in (replace(self.executable, executed=True), replace(self.executable, plan_items=())):
            with self.subTest(plan=plan), self.assertRaises(PlanError):
                self.execute(plan)

    def test_changed_approved_pack_hash_blocks_previous_run(self):
        changed = replace(self.strategy, version=2)
        registry = approved_registry((StrategyPack("codex", (changed,)),))
        self.assertIn("strategy_hash_mismatch", self.assert_live_blocked(registry=registry))

    def test_risk_consent_must_be_present_again(self):
        self.assertIn("requires_include_confirm", self.assert_live_blocked(include_confirm=False))

    def test_non_boolean_consent_rejected(self):
        for value in (1, "true", None):
            with self.subTest(value=value), self.assertRaises(PlanError):
                self.plan(user_confirmed=value)

    def test_runtime_upgrade_requires_new_inspect(self):
        run = replace(self.result.run, openclean_version="0.0.0")
        self.assertIn("runtime_version_mismatch", self.assert_live_blocked(run=run))

    def test_two_findings_cannot_schedule_the_same_path_twice(self):
        first = self.result.findings[0]
        second = replace(first, finding_id="finding:duplicate-path")
        run = replace(self.result.run, finding_ids=(first.finding_id, second.finding_id))
        with self.assertRaisesRegex(PlanError, "duplicate filesystem"):
            self.plan(run=run, findings=(first, second), selected_ids=run.finding_ids)
