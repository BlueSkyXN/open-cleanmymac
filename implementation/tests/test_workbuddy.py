from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from openclean.cli import main
from openclean.engine import IgnoreRules, finalize_overlapping_result
from openclean.models import FileFacts, ScanResult
from openclean.processes import OpenFileSnapshot, ProcessSnapshot
from openclean.storage_diagnostics import RetentionRule, scan_retention_rules
from openclean.workbuddy import scan_workbuddy_storage


class WorkBuddyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name) / "home"
        self.home.mkdir()
        self.base = self.home / ".workbuddy"
        self.now = time.time()
        self.snapshots = (ProcessSnapshot(()), OpenFileSnapshot(()))

    def file(self, relative):
        path = self.base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture" * 1024)
        return path

    def scan(self, **kwargs):
        return scan_workbuddy_storage(IgnoreRules(), home=self.home, snapshots=self.snapshots,
                                     now=self.now, **kwargs)

    def test_expired_suffix_exact_cache_and_protected_data(self):
        expected = [
            self.file("logs/2026-01-01.expired-1700000000000-a1b2c3d4/data"),
            self.file("app/session/Cache/data"),
            self.file("app/session/Partitions/profile/Code Cache/data"),
        ]
        for relative in ("logs/.expired-1700000000000-a1b2c3d4/data",
                         "logs/2026-02-31.expired-1700000000000-a1b2c3d4/data",
                         "logs/2026-01-01/data", "logs/sandbox/data", "traces/not-a-worker/data",
                         "app/session/IndexedDB/data", "app/session/Local Storage/data",
                         "binaries/python/versions/current/data", "plugins/cache/data",
                         "connectors-marketplace/data", "skills/data", "file-history/data"):
            self.file(relative)
        result = self.scan()
        self.assertTrue(result.complete)
        self.assertEqual({item.path for item in result.items}, {path.parent for path in expected})
        self.assertTrue(all(not item.actionable and not item.preselected for item in result.items))
        self.assertTrue(all(path.read_bytes() == b"fixture" * 1024 for path in expected))

    def test_worker_age_uses_newest_directory_as_well_as_files(self):
        data = self.file("traces/123/nested/data")
        worker = self.base / "traces/123"
        old = self.now - 40 * 86400
        for path in (data, data.parent, worker):
            os.utime(path, (old, old))
        result = self.scan()
        self.assertEqual(result.items[0].age_days, 40)
        os.utime(data.parent, (self.now - 86400, self.now - 86400))
        result = self.scan()
        self.assertEqual(result.items[0].age_days, 1)
        self.assertGreater(result.items[0].retention_30d_bytes, 0)
        self.assertIn("不能按单文件", result.items[0].note)
        self.assertFalse(result.items[0].actionable)

    def test_symlink_and_ignore_are_applied_before_directory_enumeration(self):
        data = self.file("logs/2026-01-01.expired-1700000000000-a1b2c3d4/data")
        outside = self.home / "outside"
        outside.mkdir()
        (self.base / "traces").symlink_to(outside, target_is_directory=True)
        result = scan_workbuddy_storage(IgnoreRules((str(self.base / "logs"),)),
                                       home=self.home, snapshots=self.snapshots)
        self.assertFalse(result.items)
        self.assertTrue(data.exists())
        self.assertIn("unsafe_symlink_ancestor", [issue.code for issue in result.issues])

    def test_discovery_and_measurement_limits_report_incomplete(self):
        data = self.file("logs/2026-01-01.expired-1700000000000-a1b2c3d4/data")
        self.assertFalse(self.scan(max_entries=0).complete)
        self.file("logs/2026-01-01.expired-1700000000000-a1b2c3d4/second")
        result = scan_retention_rules((RetentionRule("limit", str(data.parent), ()),),
            IgnoreRules(), process_snapshot=self.snapshots[0], open_files=self.snapshots[1], entry_limit=1)
        self.assertFalse(result.complete)
        self.assertEqual(result.items[0].retention_file_count, 1)

    def test_worker_group_age_is_unknown_when_protected_children_are_skipped(self):
        old = self.file("traces/123/old")
        protected = self.file("traces/123/keep")
        when = self.now - 40 * 86400
        for path in (old, protected, old.parent):
            os.utime(path, (when, when))
        os.utime(protected, (self.now, self.now))
        result = scan_workbuddy_storage(IgnoreRules((str(protected),)), home=self.home,
                                       snapshots=self.snapshots, now=self.now)
        self.assertIsNone(result.items[0].age_days)
        self.assertIsNone(result.items[0].latest_mtime)
        self.assertIn("整组年龄未知", result.items[0].note)

    def test_cloud_child_is_counted_and_group_age_is_not_guessed(self):
        self.file("traces/123/old")
        cloud = self.file("traces/123/cloud")
        with mock.patch.object(FileFacts, "is_probable_cloud_placeholder",
                               property(lambda facts: facts.path == cloud)):
            result = self.scan()
        self.assertEqual(result.items[0].cloud_file_count, 1)
        self.assertIsNone(result.items[0].age_days)
        self.assertFalse(result.items[0].actionable)

    def test_parent_retention_is_not_double_counted(self):
        self.file("logs/2026-01-01.expired-1700000000000-a1b2c3d4/data")
        self.file("logs/current/data")
        parent = scan_retention_rules((RetentionRule("all logs", str(self.base / "logs"), ()),),
            IgnoreRules(), process_snapshot=self.snapshots[0], open_files=self.snapshots[1])
        combined = finalize_overlapping_result(ScanResult(items=parent.items + self.scan().items))
        self.assertEqual(combined.total, parent.total)
        self.assertEqual(combined.actionable_total, 0)

    def test_running_and_unknown_processes_remain_readonly(self):
        self.file("app/session/Cache/data")
        for snapshots in ((ProcessSnapshot(("/Apps/WorkBuddy.app/main",)), OpenFileSnapshot(())),
                          (None, None)):
            result = scan_workbuddy_storage(IgnoreRules(), home=self.home, snapshots=snapshots)
            self.assertFalse(result.items[0].actionable)
            self.assertIn("正在运行" if snapshots[0] else "进程状态未知", result.items[0].note)

    def test_both_bundle_migration_ids_have_exact_application_ownership(self):
        from openclean.application_ownership import process_markers_for_path
        for name in ("com.workbuddy.workbuddy.BundleMigration", "com.tencent.workbuddy.mac.BundleMigration"):
            path = self.home / "Library/Caches" / name
            self.assertIn("WorkBuddy.app", process_markers_for_path(path, home=self.home))
            self.assertFalse(process_markers_for_path(path.with_name(name + "-unrelated"), home=self.home))

    def test_agent_inspect_show_and_blocked_cleanup_use_real_ids(self):
        data = self.file("logs/2026-01-01.expired-1700000000000-a1b2c3d4/data")
        state = str(self.home / "runs")

        def call(args):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                status = main([*args, "--json"])
            return status, json.loads(stdout.getvalue())

        with mock.patch.dict(os.environ, {"HOME": str(self.home)}), \
                mock.patch("openclean.runtime.inspect_service._capture_snapshots", return_value=self.snapshots):
            status, inspected = call(["inspect", "workbuddy", "--run-store", state])
            self.assertEqual(status, 0)
            self.assertEqual(inspected["totals"]["actionable"], 0)
            self.assertEqual(inspected["totals"]["findings"], 1)
            run = inspected["run_id"]
            finding = inspected["findings"][0]["finding_id"]
            status, shown = call(["show", "--run", run, "--finding", finding, "--run-store", state])
            self.assertEqual(status, 0)
            self.assertIn("expired", json.dumps(shown))
            status, planned = call(["clean", "--run", run, "--finding", finding, "--run-store", state])
            self.assertEqual(status, 0)
            self.assertFalse(planned["executed"])
            status, blocked = call(["clean", "--run", run, "--finding", finding,
                                    "--run-store", state, "--yes", "--include-critical"])
            self.assertEqual(status, 1)
            self.assertFalse(blocked["executed"])
        self.assertTrue(data.exists())

    def test_classic_ai_scan_includes_same_experience(self):
        self.file("app/session/GPUCache/data")
        from openclean.models import ScanResult
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}), \
                mock.patch("openclean.workbuddy._capture_runtime_snapshots", return_value=self.snapshots), \
                mock.patch("openclean.engine.scan_sqlite_diagnostics", return_value=ScanResult()), \
                mock.patch("openclean.engine.scan_codex_storage_artifact_diagnostics", return_value=ScanResult()), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            status = main(["scan", "--domain", "ai", "--json"])
        self.assertEqual(status, 0)
        self.assertTrue(any(item["category"] == "WorkBuddy Electron 精确缓存"
                            for item in json.loads(output.getvalue())["items"]))
