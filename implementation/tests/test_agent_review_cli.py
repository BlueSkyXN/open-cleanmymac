"""Public command contracts: isolated HOME, structured failures and no implicit actions."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, replace
from unittest import mock

from agent_fixtures import AgentFixture
from openclean.cli import CLI_SCHEMA_VERSION
from openclean.core.errors import StrategyError
from openclean.core.models import RunIssue, StrategyPack
from openclean.redaction import redact_json_payload
from openclean.runtime.run_store import RunStore, default_run_store_dir
from openclean.strategies.registry import StrategyRegistry, load_pack, pack_from_mapping


class ReviewCliTests(AgentFixture):
    def test_builtin_pack_has_no_production_action(self):
        registry = StrategyRegistry.load()
        strategies = registry.runtime_visible("codex")
        self.assertEqual(len(strategies), 7)
        self.assertTrue(all(s.status == "active" and not s.action.supported for s in strategies))
        self.assertTrue(all(not registry.action_approved(s) for s in strategies))

    def test_string_boolean_and_unknown_fields_in_pack_rejected(self):
        for field, value in (("supported", "false"), ("unexpected", False)):
            with self.subTest(field=field):
                payload = asdict(self.pack)
                payload["strategies"][0]["action"][field] = value
                with self.assertRaises(StrategyError):
                    pack_from_mapping(payload)

    def test_negative_age_and_noninteger_version_rejected(self):
        for field, value in (("version", True), ("version", 1.2)):
            with self.subTest(value=value):
                payload = asdict(self.pack)
                payload["strategies"][0][field] = value
                with self.assertRaises(StrategyError):
                    pack_from_mapping(payload)
        payload = asdict(self.pack)
        payload["strategies"][0]["conditions"]["minimum_age_days"] = -1
        with self.assertRaises(StrategyError):
            pack_from_mapping(payload)

    def test_path_traversal_pack_name_rejected(self):
        for name in ("../codex", "/codex", "codex.json", "", "a/b"):
            with self.subTest(name=name), self.assertRaises(StrategyError):
                load_pack(name)

    def test_invalid_utf8_pack_is_structured_error(self):
        packs = self.root / "packs"
        packs.mkdir()
        (packs / "codex.json").write_bytes(b"\xff")
        code, payload = self.cli(["strategy", "verify", "--packs-dir", str(packs), "--json"])
        self.assertEqual(code, 2)
        self.assertIn("error", payload)
        self.assertEqual(payload["schema_version"], CLI_SCHEMA_VERSION)

    def test_home_override_controls_paths_store_and_rules(self):
        decoy = self.root / "actual-home"
        decoy.mkdir()
        # Bad rules in the real HOME must not be loaded for --home's fixture.
        config = decoy / ".config/openclean"
        config.mkdir(parents=True)
        (config / "rules.json").write_text("not JSON")
        with mock.patch.dict(os.environ, {"HOME": str(decoy)}):
            code, payload = self.cli(["inspect", "codex", "--home", str(self.home), "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["home"], str(self.home))
        self.assertEqual(payload["run_store"], str(self.home / ".local/state/openclean/runs"))
        self.assertTrue(payload["developer_mode"])
        self.assertFalse((decoy / ".local").exists())
        self.assertTrue(all(f["display_path"].startswith(str(self.home)) for f in payload["findings"]))

    def test_ignore_applies_to_agent_detector(self):
        with mock.patch("openclean.cli._load_registry", return_value=self.registry):
            code, payload = self.cli(["inspect", "codex", "--ignore", str(self.target), "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["totals"]["actionable"], 0)
        self.assertTrue(self.target.exists())

    def test_invalid_rules_returns_json_not_traceback(self):
        rules = self.root / "rules.json"
        rules.write_text("not JSON")
        code, payload = self.cli(["inspect", "codex", "--rules", str(rules), "--json"])
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"]["code"], "rules_error")
        self.assertEqual(payload["schema_version"], CLI_SCHEMA_VERSION)

    def test_unsupported_detector_produces_incomplete_exit_one(self):
        strategy = replace(self.strategy, detector=replace(self.strategy.detector, name="filesystem_tree"))
        registry = StrategyRegistry((StrategyPack("codex", (strategy,)),))
        with mock.patch("openclean.cli._load_registry", return_value=registry):
            code, payload = self.cli(["inspect", "codex", "--json"])
        self.assertEqual(code, 1)
        self.assertFalse(payload["complete"])
        self.assertEqual(payload["status"], "incomplete")
        self.assertEqual(payload["issues"][0]["code"], "scanner_unavailable")

    def test_mixed_batch_json_never_claims_executed(self):
        self.make_target("marketplace-upgrade-recent", days=0)
        result = self.inspect()
        args = ["clean", "--run", result.run.run_id, "--yes", "--include-confirm", "--json"]
        for finding in result.findings:
            args += ["--finding", finding.finding_id]
        with mock.patch("openclean.cli._load_registry", return_value=self.registry), \
             mock.patch("openclean.actions.planner.execute_cleanup") as action:
            code, payload = self.cli(args)
        action.assert_not_called()
        self.assertEqual(code, 1)
        self.assertEqual(payload["status"], "blocked")
        self.assertFalse(payload["executed"])
        self.assertFalse(payload["plan"]["executed"])
        self.assertFalse(payload["outcome"]["complete"])
        self.assertEqual(sorted(o["status"] for o in payload["outcome"]["outcomes"]), ["blocked", "not_run"])
        self.assertIn("plan_items", payload["plan"])
        self.assertNotIn("items", payload["plan"])

    def test_incomplete_run_clean_returns_specific_code(self):
        result = self.inspect()
        run = replace(result.run, complete=False, issues=(RunIssue("partial", "partial", True),))
        with mock.patch("openclean.cli._load_registry", return_value=self.registry), \
             mock.patch.object(RunStore, "load_bundle", return_value=(run, result.findings)):
            code, payload = self.cli(["clean", "--run", run.run_id, "--finding", run.finding_ids[0],
                                      "--yes", "--include-confirm", "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"]["code"], "run_incomplete")
        self.assertFalse(payload["executed"])

    def test_nondefault_run_store_execution_refused_before_load(self):
        with mock.patch.object(RunStore, "load_bundle") as read:
            code, payload = self.cli(["clean", "--run", "run:fake", "--finding", "finding:fake",
                                      "--run-store", str(self.root / "arbitrary"), "--yes", "--json"])
        read.assert_not_called()
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"]["code"], "developer_execution_disabled")

    def test_standalone_developer_options_do_not_fall_through_to_classic_clean(self):
        for flag in ("--run-store", "--packs-dir"):
            with self.subTest(flag=flag), mock.patch("openclean.cli.scan_domains") as scan:
                code, payload = self.cli(["clean", flag, str(self.root), "--yes", "--json"])
                self.assertEqual(code, 2)
                self.assertFalse(payload["executed"])
                scan.assert_not_called()

    def test_default_store_ignores_relative_xdg_value(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "relative/path"}):
            self.assertEqual(default_run_store_dir(), self.home / ".local/state/openclean/runs")

    def test_redaction_also_removes_ids_embedded_in_errors(self):
        payload = redact_json_payload({"error": {"message": "Finding finding:abc not in Run run:xyz"},
                                       "run_id": "run:xyz", "finding_ids": ["finding:abc"],
                                       "home": str(self.home)})
        raw = json.dumps(payload)
        self.assertNotIn("finding:abc", raw)
        self.assertNotIn("run:xyz", raw)
        self.assertNotIn(str(self.home), raw)
        self.assertFalse(payload["redaction"]["selection_replayable"])
