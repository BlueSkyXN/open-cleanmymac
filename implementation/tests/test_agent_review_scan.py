from __future__ import annotations

from dataclasses import fields
import json
from importlib.resources import files
from unittest import mock

from agent_fixtures import AgentFixture
from openclean.core.models import CleanupPlan, Finding, Run
from openclean.models import ScanIssue
from openclean import storage_diagnostics as diagnostics


class ReviewScanTests(AgentFixture):
    def test_target_changes_during_measurement_are_not_persisted_as_candidates(self):
        original = diagnostics._candidate_measurement
        def changing(*args, **kwargs):
            result = original(*args, **kwargs)
            (self.target / "new-file").write_bytes(b"new")
            return result
        with mock.patch.object(diagnostics, "_candidate_measurement", side_effect=changing):
            result = self.inspect()
        self.assertFalse(result.run.complete)
        self.assertFalse(result.findings)
        self.assertIn("target_changed_during_scan", [i.code for i in result.issues])

    def test_root_changes_during_scan_invalidate_entire_result(self):
        original = diagnostics._candidate_measurement
        def changing(*args, **kwargs):
            result = original(*args, **kwargs)
            (self.target.parent / "unrelated-new-entry").mkdir()
            return result
        with mock.patch.object(diagnostics, "_candidate_measurement", side_effect=changing):
            result = self.inspect()
        self.assertFalse(result.run.complete)
        self.assertFalse(result.findings)
        self.assertIn("root_changed_during_scan", [i.code for i in result.issues])

    def test_partial_measurement_prevents_action(self):
        original = diagnostics._candidate_measurement
        def partial(path, device, protection, issues, **kwargs):
            result = original(path, device, protection, issues, **kwargs)
            issues.append(ScanIssue(code="fixture_partial", message="injected partial scan", blocking=True))
            return result
        with mock.patch.object(diagnostics, "_candidate_measurement", side_effect=partial):
            result = self.inspect()
        self.assertFalse(result.run.complete)
        self.assertEqual(len(result.findings), 1)
        self.assertFalse(result.findings[0].assessment.actionable)
        self.assertIn("measurement_incomplete", result.findings[0].assessment.block_reasons[0])

    def test_enumeration_budget_prevents_action_on_prefix(self):
        self.make_target("marketplace-upgrade-b")
        with mock.patch.object(diagnostics, "_MAX_CODEX_STAGING_ROOTS", 1):
            result = self.inspect()
        self.assertFalse(result.run.complete)
        self.assertEqual(len(result.findings), 1)
        self.assertFalse(result.findings[0].assessment.actionable)

    def test_schemas_cover_complete_serialized_models(self):
        for name, cls in (("run", Run), ("finding", Finding), ("cleanup-plan", CleanupPlan)):
            with self.subTest(name=name):
                schema = json.loads((files("openclean") / "schemas" / f"{name}.json").read_text())
                expected = {field.name for field in fields(cls)}
                self.assertEqual(set(schema["properties"]), expected)
                self.assertEqual(set(schema["required"]), expected)
