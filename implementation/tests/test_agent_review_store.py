"""Persisted data is input, not execution authority; corruption must never trigger a scan."""
from __future__ import annotations

import copy
import json
import os
from dataclasses import replace
from unittest import mock

from agent_fixtures import AgentFixture
from openclean.core.errors import RunNotFoundError, RunStoreError
from openclean.runtime import run_store as storage
from openclean.runtime.run_store import RunStore


class ReviewStoreTests(AgentFixture):
    def setUp(self):
        super().setUp()
        self.result = self.inspect()
        self.run = self.result.run
        self.path = self.store._run_path(self.run.run_id)
        self.raw = self.path.read_bytes()
        self.payload = json.loads(self.raw)

    def reject(self, payload):
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(RunStoreError):
            self.store.load_bundle(self.run.run_id)

    def test_all_top_level_fields_are_required(self):
        for key in self.payload:
            with self.subTest(key=key):
                changed = copy.deepcopy(self.payload)
                del changed[key]
                self.reject(changed)

    def test_every_run_field_is_required_in_persisted_bundle(self):
        for key in self.payload["run"]:
            with self.subTest(key=key):
                changed = copy.deepcopy(self.payload)
                del changed["run"][key]
                self.reject(changed)

    def test_every_finding_field_is_required_in_persisted_bundle(self):
        for key in self.payload["findings"][0]:
            with self.subTest(key=key):
                changed = copy.deepcopy(self.payload)
                del changed["findings"][0][key]
                self.reject(changed)

    def test_every_item_field_is_required_in_evidence(self):
        for key in self.payload["findings"][0]["evidence"]["payload"]:
            with self.subTest(key=key):
                changed = copy.deepcopy(self.payload)
                del changed["findings"][0]["evidence"]["payload"][key]
                self.reject(changed)

    def test_boolean_and_number_confusion_rejected(self):
        for container, key, value in (("run", "complete", "false"), ("run", "created_at", True),
                                      ("run", "finding_ids", "finding:one"),
                                      ("run", "strategy_versions", {self.strategy.id: True})):
            with self.subTest(key=key):
                changed = copy.deepcopy(self.payload)
                changed[container][key] = value
                self.reject(changed)

    def test_nonfinite_and_overlong_timestamps_rejected(self):
        for value in (float("nan"), float("inf"), self.run.created_at + 86401,
                      self.run.created_at - 1):
            with self.subTest(value=value):
                changed = copy.deepcopy(self.payload)
                changed["run"]["expires_at"] = value
                self.reject(changed)

    def test_schema_one_and_boolean_version_rejected(self):
        for version in (1, True, 3, "2"):
            with self.subTest(version=version):
                changed = copy.deepcopy(self.payload)
                changed["schema_version"] = version
                self.reject(changed)

    def test_duplicate_json_key_rejected(self):
        self.path.write_bytes(self.raw.replace(b'"schema_version":2', b'"schema_version":1,"schema_version":2'))
        with self.assertRaisesRegex(RunStoreError, "duplicate JSON key"):
            self.store.load_bundle(self.run.run_id)

    def test_invalid_utf8_rejected(self):
        self.path.write_bytes(b'\xff')
        with self.assertRaises(RunStoreError):
            self.store.load_bundle(self.run.run_id)

    def test_cross_run_and_filename_mismatch_rejected(self):
        for section in ("run", "finding"):
            with self.subTest(section=section):
                changed = copy.deepcopy(self.payload)
                target = changed["run"] if section == "run" else changed["findings"][0]
                target["run_id"] = "run:different"
                self.reject(changed)

    def test_duplicate_findings_or_manifest_rejected(self):
        for field in ("findings", "finding_ids"):
            with self.subTest(field=field):
                changed = copy.deepcopy(self.payload)
                target = changed if field == "findings" else changed["run"]
                target[field] *= 2
                self.reject(changed)

    def test_semantic_target_assessment_measurement_binding(self):
        cases = (("target", "display_path", str(self.home / "unrelated")),
                 ("assessment", "action_risk", "safe"),
                 ("measurement", "size", 123),
                 ("evidence", "kind", "sqlite_freelist"))
        for section, key, value in cases:
            with self.subTest(section=section):
                changed = copy.deepcopy(self.payload)
                changed["findings"][0][section][key] = value
                self.reject(changed)

    def test_filesystem_identity_and_version_required(self):
        for section, key, value in (("target", "identity", None),
                                   ("target", "identity", {"device": 1, "inode": -1, "owner": 0})):
            with self.subTest(value=value):
                changed = copy.deepcopy(self.payload)
                changed["findings"][0][section][key] = value
                self.reject(changed)
        changed = copy.deepcopy(self.payload)
        changed["run"]["strategy_versions"] = {}
        self.reject(changed)

    def test_redacted_and_traversal_run_ids_rejected(self):
        for identifier in ("run:redacted", "run:../x", "run:a/b", "run:", "run:" + "x" * 129):
            with self.subTest(identifier=identifier), self.assertRaises(RunStoreError):
                self.store.load_bundle(identifier)

    def test_single_file_load_binds_complete_bundle(self):
        with mock.patch.object(self.store, "_read_at", wraps=self.store._read_at) as read:
            run, findings = self.store.load_bundle(self.run.run_id)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(set(run.finding_ids), {f.finding_id for f in findings})

    def test_existing_run_cannot_be_overwritten(self):
        with self.assertRaises(RunStoreError):
            self.store.save(self.run, self.result.findings)
        self.assertEqual(self.path.read_bytes(), self.raw)

    def test_symlink_file_cannot_redirect_read(self):
        other = self.root / "other.json"
        self.path.rename(other)
        self.path.symlink_to(other)
        with self.assertRaises(RunStoreError):
            self.store.load_bundle(self.run.run_id)
        self.assertEqual(other.read_bytes(), self.raw)

    def test_hardlinked_file_rejected(self):
        os.link(self.path, self.root / "hardlink.json")
        with self.assertRaisesRegex(RunStoreError, "硬链接"):
            self.store.load_bundle(self.run.run_id)

    def test_fifo_does_not_block_reader(self):
        self.path.unlink()
        os.mkfifo(self.path, 0o600)
        with self.assertRaises(RunStoreError):
            self.store.load_bundle(self.run.run_id)

    def test_ancestor_symlink_cannot_redirect_save(self):
        destination = self.root / "elsewhere"
        destination.mkdir(mode=0o700)
        link = self.root / "link"
        link.symlink_to(destination, target_is_directory=True)
        with self.assertRaises(RunStoreError):
            RunStore(link / "runs").save(self.run, self.result.findings)
        self.assertEqual(list(destination.iterdir()), [])

    def test_capacity_rejects_oversize_before_writing(self):
        store = RunStore(self.root / "new-store")
        with mock.patch.object(storage, "MAX_RUN_BYTES", 64), self.assertRaises(RunStoreError):
            store.save(self.run, self.result.findings)
        self.assertFalse(store.directory.exists())

    def test_total_byte_capacity_evicts_oldest_written_bundle(self):
        # Both bundles are individually legal, but the budget fits only one.
        with mock.patch.object(storage, "MAX_TOTAL_BYTES", len(self.raw) + 128):
            second = self.inspect()
        self.assertEqual(len(list(self.store.directory.glob("*.json"))), 1)
        self.assertEqual(self.store.load_run(second.run.run_id), second.run)
        with self.assertRaises(RunNotFoundError):
            self.store.load_bundle(self.run.run_id)

    def test_failed_replace_cleans_temporary_file(self):
        fresh = replace(self.run, run_id="run:replacement-failure", finding_ids=())
        with mock.patch.object(storage.os, "replace", side_effect=OSError("injected")):
            with self.assertRaises(RunStoreError):
                self.store.save(fresh, ())
        self.assertEqual([p.name for p in self.store.directory.iterdir()], [self.path.name])
        self.assertEqual(self.path.read_bytes(), self.raw)

    def test_malformed_bundle_never_causes_implicit_inspect(self):
        self.path.write_text("{}")
        with mock.patch("openclean.cli.inspect_target") as scan:
            code, payload = self.cli(["clean", "--run", self.run.run_id, "--finding",
                                      self.run.finding_ids[0], "--json"])
        self.assertNotEqual(code, 0)
        self.assertFalse(payload["executed"])
        scan.assert_not_called()
