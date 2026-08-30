from __future__ import annotations

import os
import plistlib
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from openclean.cli import _item_payload
from openclean.engine import IgnoreRules
from openclean.knowledge_base import KnowledgeBase
from openclean.macos import DarwinUserTempDiscovery
from openclean.processes import OpenFileSnapshot, ProcessSnapshot
from openclean.storage_diagnostics import (
    RetentionRule,
    SQLiteRule,
    scan_darwin_temp_updater_diagnostics,
    scan_retention_rules,
    scan_sqlite_rules,
)


class RetentionDiagnosticTests(unittest.TestCase):
    def test_knowledge_base_protection_short_circuits_before_lstat(self) -> None:
        protected = Path("/protected/logs")
        gate = IgnoreRules(
            knowledge_base=KnowledgeBase.from_mapping(
                {
                    "schema_version": 1,
                    "protect": {"paths": [str(protected)]},
                }
            )
        )

        with mock.patch(
            "openclean.storage_diagnostics.lstat_retry",
            side_effect=AssertionError("protected path must not be inspected"),
        ):
            result = scan_retention_rules(
                (RetentionRule("Test logs", str(protected), ("app",)),),
                gate,
                process_snapshot=ProcessSnapshot(()),
                open_files=OpenFileSnapshot(()),
            )

        self.assertEqual(result.items, [])
        self.assertEqual(result.issues, [])

    def test_reports_physical_age_buckets_without_becoming_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            now = time.time()
            files = []
            for index, days in enumerate((8, 15, 31), start=1):
                path = root / f"{index}.log"
                path.write_bytes(bytes([index]) * 8192)
                timestamp = now - days * 86400
                os.utime(path, (timestamp, timestamp))
                files.append(path)

            result = scan_retention_rules(
                (RetentionRule("Test logs", str(root), ("test-app",)),),
                IgnoreRules(),
                process_snapshot=ProcessSnapshot(("/Applications/Test-App",)),
                open_files=OpenFileSnapshot((str(files[0]),)),
                now=now,
            )

            self.assertTrue(result.complete)
            self.assertEqual(len(result.items), 1)
            item = result.items[0]
            allocated = [path.stat().st_blocks * 512 for path in files]
            self.assertEqual(item.size, sum(allocated))
            self.assertEqual(item.retention_file_count, 3)
            self.assertEqual(item.retention_7d_bytes, sum(allocated))
            self.assertEqual(item.retention_14d_bytes, sum(allocated[1:]))
            self.assertEqual(item.retention_30d_bytes, allocated[2])
            self.assertEqual(item.open_handle_count, 1)
            self.assertEqual(item.diagnostic_kind, "retention")
            self.assertFalse(item.actionable)
            self.assertFalse(item.preselected)
            self.assertIn("正在运行", item.note)
            payload = _item_payload(item)
            self.assertEqual(payload["retention_30d_bytes"], allocated[2])
            self.assertEqual(payload["diagnostic_kind"], "retention")

    def test_ignored_root_is_not_inspected_or_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "protected-logs"
            root.mkdir()
            (root / "secret.log").write_text("not read", encoding="utf-8")

            result = scan_retention_rules(
                (RetentionRule("Test logs", str(root), ("app",)),),
                IgnoreRules([str(root)]),
                process_snapshot=ProcessSnapshot(()),
                open_files=OpenFileSnapshot(()),
            )

            self.assertEqual(result.items, [])
            self.assertEqual(result.issues, [])


class SQLiteDiagnosticTests(unittest.TestCase):
    def _database_with_freelist(self, path: Path) -> None:
        connection = sqlite3.connect(path)
        try:
            connection.execute("CREATE TABLE payloads(value BLOB)")
            connection.executemany(
                "INSERT INTO payloads VALUES (?)",
                ((b"x" * 4096,) for _ in range(512)),
            )
            connection.commit()
            connection.execute("DELETE FROM payloads")
            connection.commit()
        finally:
            connection.close()

    def test_immutable_read_only_scan_reports_freelist_without_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "logs.sqlite"
            self._database_with_freelist(database)
            sidecars = tuple(Path(f"{database}{suffix}") for suffix in ("-wal", "-shm"))
            self.assertFalse(any(path.exists() for path in sidecars))

            result = scan_sqlite_rules(
                (
                    SQLiteRule(
                        "Test SQLite",
                        str(database),
                        ("test-app",),
                        minimum_free_bytes=1,
                    ),
                ),
                IgnoreRules(),
                process_snapshot=ProcessSnapshot(("test-app",)),
                open_files=OpenFileSnapshot((str(database),)),
            )

            self.assertTrue(result.complete)
            self.assertEqual(len(result.items), 1)
            item = result.items[0]
            self.assertEqual(item.diagnostic_kind, "sqlite_freelist")
            self.assertGreater(item.sqlite_freelist_count or 0, 0)
            self.assertGreater(item.sqlite_internal_free_bytes or 0, 0)
            self.assertGreater(item.sqlite_internal_free_ratio or 0, 0)
            self.assertLessEqual(item.size, item.allocated_size or 0)
            self.assertEqual(item.open_handle_count, 1)
            self.assertFalse(item.actionable)
            self.assertFalse(any(path.exists() for path in sidecars))
            payload = _item_payload(item)
            self.assertEqual(payload["diagnostic_kind"], "sqlite_freelist")
            self.assertEqual(
                payload["sqlite_internal_free_bytes"],
                item.sqlite_internal_free_bytes,
            )

    def test_non_sqlite_file_is_a_nonblocking_diagnostic_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "not-a-database.sqlite"
            database.write_bytes(b"not sqlite")

            result = scan_sqlite_rules(
                (
                    SQLiteRule(
                        "Test SQLite",
                        str(database),
                        ("test-app",),
                        minimum_free_bytes=1,
                    ),
                ),
                IgnoreRules(),
                process_snapshot=ProcessSnapshot(()),
                open_files=OpenFileSnapshot(()),
            )

            self.assertTrue(result.complete)
            self.assertEqual(result.items, [])
            self.assertEqual(result.issues[0].code, "sqlite_diagnostic_failed")
            self.assertFalse(result.issues[0].blocking)


class DarwinTempUpdaterDiagnosticTests(unittest.TestCase):
    def test_dynamic_shipit_copy_is_visible_but_never_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp) / "T"
            staging = temp_root / "com.aliyun.lingma.ide.ShipIt.test"
            contents = staging / "Qoder CN IDE.app/Contents"
            contents.mkdir(parents=True)
            (contents / "Info.plist").write_bytes(
                plistlib.dumps(
                    {
                        "CFBundleIdentifier": "com.aliyun.lingma.ide",
                        "CFBundleShortVersionString": "1.27.0",
                    }
                )
            )
            (contents / "payload.bin").write_bytes(b"payload")

            with mock.patch(
                "openclean.storage_diagnostics.discover_darwin_user_temp",
                return_value=DarwinUserTempDiscovery(paths=(temp_root,)),
            ), mock.patch(
                "openclean.storage_diagnostics.capture_process_snapshot",
                return_value=ProcessSnapshot(()),
            ), mock.patch(
                "openclean.storage_diagnostics.capture_open_file_snapshot",
                return_value=OpenFileSnapshot(()),
            ), mock.patch(
                "openclean.updater._application_roots",
                return_value=(Path(tmp) / "Applications",),
            ):
                result = scan_darwin_temp_updater_diagnostics(IgnoreRules())

            self.assertTrue(result.complete)
            self.assertEqual(len(result.items), 1)
            item = result.items[0]
            self.assertEqual(item.diagnostic_kind, "updater_temp")
            self.assertEqual(item.updater_status, "installed_app_missing")
            self.assertEqual(item.staged_version, "1.27.0")
            self.assertFalse(item.actionable)
            self.assertFalse(item.preselected)


if __name__ == "__main__":
    unittest.main()
