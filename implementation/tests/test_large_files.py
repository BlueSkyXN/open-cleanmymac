from __future__ import annotations

import contextlib
import errno
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from openclean.cleanup import execute_cleanup
from openclean.cli import main
from openclean.engine import Control, IgnoreRules
from openclean.filesystem import filesystem_id_retry, lstat_retry, scandir_entries
from openclean.knowledge_base import KnowledgeBase
from openclean.large_files import LargeFilesError, scan_large_files


class LargeFilesTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.root = self.home / "files"
        self.root.mkdir()
        self.rules = self.home / "rules.json"
        self.rules.write_text('{"schema_version": 1}', encoding="utf-8")
        environment = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)

    def file(self, name: str, size: int) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        return path

    def cli(self, *arguments: str):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = main(["large", str(self.root), "--rules", str(self.rules), "--json", *arguments])
        return status, json.loads(output.getvalue()), output.getvalue()

    def test_recursive_threshold_top_and_totals_are_file_based_and_readonly(self) -> None:
        small = self.file("small.bin", 99)
        boundary = self.file("nested/boundary.bin", 100)
        biggest = self.file("nested/deep/large.bin", 1000)
        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("must not read content")):
            result = scan_large_files(self.root, min_size=100, top=1)
        self.assertTrue(result.complete)
        self.assertTrue(result.truncated)
        self.assertEqual([item.path for item in result.items], [biggest])
        self.assertEqual(result.scanned_files, 3)
        self.assertEqual(result.matched_files, 2)
        self.assertEqual(result.logical_bytes, 1100)
        self.assertEqual(result.allocated_bytes, (boundary.stat().st_blocks + biggest.stat().st_blocks) * 512)
        item, = result.items
        self.assertFalse(item.actionable)
        self.assertFalse(item.preselected)
        report = execute_cleanup([item], IgnoreRules(), home=self.home)
        self.assertFalse(report.complete)
        self.assertTrue(small.exists() and boundary.exists() and biggest.exists())
        self.assertFalse((self.home / ".Trash").exists())
        self.assertEqual(len(scan_large_files(self.root, min_size=100, top=0).items), 2)

    def test_sparse_and_hardlinked_files_separate_logical_and_allocated_size(self) -> None:
        original = self.file("large.bin", 4096)
        with original.open("r+b") as stream:
            stream.truncate(2 * 1024 * 1024)
        alias = self.root / "alias.bin"
        os.link(original, alias)
        result = scan_large_files(self.root, min_size=1024 * 1024)
        self.assertEqual(result.matched_files, 1)
        self.assertEqual(result.skipped["hardlink_aliases"], 1)
        self.assertEqual(result.logical_bytes, original.stat().st_size)
        self.assertEqual(result.allocated_bytes, original.stat().st_blocks * 512)
        self.assertIn(result.items[0].path, (original, alias))

    def test_rules_prune_subtrees_before_scandir_or_stat(self) -> None:
        keep = self.file("keep.bin", 200)
        private = self.file("protected/private.bin", 400)
        protection = IgnoreRules(knowledge_base=KnowledgeBase.from_mapping({
            "schema_version": 1, "protect": {"paths": [str(private.parent)]},
        }))

        def stat_without_private(path):
            self.assertFalse(path.is_relative_to(private.parent))
            return lstat_retry(path)

        with mock.patch("openclean.engine.lstat_retry", side_effect=stat_without_private):
            result = scan_large_files(self.root, min_size=1, protection=protection)
        self.assertEqual([item.path for item in result.items], [keep])
        self.assertEqual(result.skipped["ignored"], 1)
        self.assertTrue(result.complete)
        with self.assertRaises(LargeFilesError):
            scan_large_files(private.parent, protection=protection)

    def test_symlinks_are_skipped_and_symlinked_roots_rejected(self) -> None:
        outside = self.home / "outside"
        outside.mkdir()
        (outside / "private.bin").write_bytes(b"x" * 1024)
        link = self.root / "linked"
        link.symlink_to(outside, target_is_directory=True)
        (self.root / "file-link").symlink_to(outside / "private.bin")
        result = scan_large_files(self.root, min_size=1)
        self.assertEqual(result.items, [])
        self.assertEqual(result.skipped["symlinks"], 2)
        for target in (link, link / "subdir"):
            with self.subTest(target=target):
                with self.assertRaises(LargeFilesError) as raised:
                    scan_large_files(target)
                message = str(raised.exception)
                self.assertIn("符号链接", message)
                self.assertIn(str(os.path.realpath(target)), message)
                self.assertIn("重试", message)

    def test_cloud_directories_files_and_root_ancestors_are_not_enumerated(self) -> None:
        cloud_file = self.file("cloud-file.bin", 100)
        cloud_dir = self.file("cloud-dir/private.bin", 200).parent
        kept = self.file("kept.bin", 100)

        def flagged(path):
            original = lstat_retry(path)
            if path not in (cloud_file, cloud_dir):
                return original
            return SimpleNamespace(**{key: getattr(original, key) for key in dir(original)
                                      if key.startswith("st_") and key != "st_flags"}, st_flags=0x40000000)

        def guarded_scandir(path):
            self.assertNotEqual(path, cloud_dir)
            return scandir_entries(path)

        with mock.patch("openclean.engine.lstat_retry", side_effect=flagged), mock.patch(
            "openclean.large_files.lstat_retry", side_effect=flagged,
        ), mock.patch("openclean.large_files.scandir_entries", side_effect=guarded_scandir):
            result = scan_large_files(self.root, min_size=1)
            self.assertEqual([item.path for item in result.items], [kept])
            self.assertEqual(result.skipped["cloud_placeholders"], 2)
            for root in (cloud_dir, cloud_dir / "nested"):
                with self.subTest(root=root), self.assertRaisesRegex(LargeFilesError, "dataless"):
                    scan_large_files(root)

    def test_fsid_boundary_stops_before_child_enumeration(self) -> None:
        mounted = self.file("mounted/large.bin", 1000).parent
        kept = self.file("kept.bin", 100)
        root_fsid = filesystem_id_retry(self.root)
        with mock.patch("openclean.large_files.filesystem_id_retry",
                        side_effect=lambda p: root_fsid + 1 if p == mounted else root_fsid), mock.patch(
            "openclean.large_files.scandir_entries", wraps=scandir_entries,
        ) as enumeration:
            result = scan_large_files(self.root, min_size=1)
        self.assertEqual([item.path for item in result.items], [kept])
        self.assertEqual(result.skipped["cross_filesystem"], 1)
        self.assertNotIn(mock.call(mounted), enumeration.call_args_list)

    def test_device_boundary_skips_foreign_files_and_directories(self) -> None:
        foreign = self.file("foreign/large.bin", 1000).parent
        kept = self.file("kept.bin", 100)

        def foreign_stat(path):
            original = lstat_retry(path)
            if path != foreign:
                return original
            return SimpleNamespace(**{key: getattr(original, key) for key in dir(original)
                                      if key.startswith("st_") and key != "st_dev"}, st_dev=original.st_dev + 1)

        with mock.patch("openclean.engine.lstat_retry", side_effect=foreign_stat):
            result = scan_large_files(self.root, min_size=1)
        self.assertEqual([item.path for item in result.items], [kept])
        self.assertEqual(result.skipped["cross_filesystem"], 1)

    def test_disappeared_file_returns_partial_json_and_nonzero_exit(self) -> None:
        missing = self.file("disappearing.bin", 1000)
        kept = self.file("kept.bin", 200)

        def disappearing(path):
            if path == missing:
                raise FileNotFoundError(errno.ENOENT, "disappeared", str(path))
            return lstat_retry(path)

        with mock.patch("openclean.engine.lstat_retry", side_effect=disappearing):
            status, payload, _ = self.cli("--min-size", "1B")
        self.assertEqual(status, 1)
        self.assertFalse(payload["complete"])
        self.assertEqual(payload["items"][0]["path"], str(kept))
        self.assertEqual(payload["issues"][0]["code"], "path_disappeared")
        self.assertEqual(payload["skipped"]["unreadable"], 1)
        self.assertEqual(payload["skipped"]["ignored"], 0)

    def test_permission_denied_preserves_other_matches(self) -> None:
        denied = self.file("denied/private.bin", 1000).parent
        kept = self.file("kept.bin", 200)

        def restricted(path):
            if path == denied:
                raise PermissionError(errno.EACCES, "denied", str(path))
            return scandir_entries(path)

        with mock.patch("openclean.large_files.scandir_entries", side_effect=restricted):
            result = scan_large_files(self.root, min_size=1)
        self.assertFalse(result.complete)
        self.assertEqual([item.path for item in result.items], [kept])
        self.assertEqual(result.issues[0].code, "permission_denied")
        self.assertEqual(result.skipped["unreadable"], 1)

    def test_entry_budget_is_distinct_from_top_and_exact_budget_can_complete(self) -> None:
        for index in range(4):
            self.file(f"{index}.bin", 100 + index)
        status, payload, _ = self.cli("--min-size", "1", "--top", "1", "--max-entries", "2")
        self.assertEqual(status, 1)
        self.assertFalse(payload["complete"])
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["matched_file_count"], 2)
        self.assertEqual(payload["scanned_entries"], 2)
        self.assertEqual(payload["issues"][0]["code"], "scan_limit_reached")
        result = scan_large_files(self.root, min_size=1, max_entries=4, top=1)
        self.assertTrue(result.complete)
        self.assertEqual(result.matched_files, 4)
        self.assertEqual(result.items[0].path.name, "3.bin")

    def test_cancellation_keeps_partial_results_and_exit_130(self) -> None:
        for index in range(4):
            self.file(f"{index}.bin", 100)
        control = Control()
        checkpoints = 0

        def cancel_during_scan():
            nonlocal checkpoints
            checkpoints += 1
            if checkpoints == 4:
                control.cancel()
            Control.checkpoint(control)

        with mock.patch.object(control, "checkpoint", side_effect=cancel_during_scan):
            result = scan_large_files(self.root, min_size=1, control=control)
        self.assertTrue(result.cancelled)
        self.assertFalse(result.complete)
        self.assertEqual(result.matched_files, 2)
        with mock.patch("openclean.cli.scan_large_files", return_value=result):
            status, payload, _ = self.cli()
        self.assertEqual(status, 130)
        self.assertTrue(payload["cancelled"])
        self.assertEqual(payload["matched_file_count"], 2)

    def test_cli_units_defaults_redaction_and_text(self) -> None:
        self.file("large private.bin", 2048)
        for size, expected in (("1KiB", 1024), ("1.5KB", 1500), ("100B", 100), ("2KiB", 2048)):
            with self.subTest(size=size):
                status, payload, raw = self.cli("--min-size", size, "--redact-paths")
                self.assertEqual(status, 0)
                self.assertEqual(payload["schema_version"], 2)
                self.assertEqual(payload["command"], "large")
                self.assertEqual(payload["min_size_bytes"], expected)
                self.assertEqual(payload["matched_file_count"], 1)
                self.assertEqual(payload["reclaimable_bytes"], 0)
                self.assertEqual(payload["preselected_bytes"], 0)
                self.assertNotIn(str(self.home), raw)
                self.assertNotIn("large private.bin", raw)
        _, payload, _ = self.cli()
        self.assertEqual(payload["min_size_bytes"], 100 * 1024 * 1024)
        self.assertEqual(payload["top"], 50)
        self.assertEqual(payload["max_entries"], 200_000)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = main(["large", str(self.root), "--rules", str(self.rules), "--min-size", "1"])
        self.assertEqual(status, 0)
        self.assertIn("表观大小", output.getvalue())
        self.assertIn("阈值 1 B", output.getvalue())
        self.assertIn("扫描完整", output.getvalue())
        self.assertIn("可回收量 0 B", output.getvalue())

    def test_invalid_options_and_write_flags_fail_before_scanning(self) -> None:
        cases = (("--yes",), ("--select", "target"), ("--min-size", "NaN"), ("--min-size", "0"),
                 ("--min-size", "-1MiB"), ("--top", "-1"), ("--max-entries", "0"))
        with mock.patch("openclean.cli.scan_large_files") as scanner:
            for options in cases:
                with self.subTest(options=options):
                    status, payload, _ = self.cli(*options)
                    self.assertEqual(status, 2)
                    self.assertEqual(payload["command"], "large")
                    self.assertEqual(payload["error"]["code"], "usage_error")
            scanner.assert_not_called()

    def test_missing_root_and_invalid_rules_have_redacted_errors(self) -> None:
        self.root.rmdir()
        status, payload, raw = self.cli("--redact-paths")
        self.assertEqual(status, 2)
        self.assertEqual(payload["error"]["code"], "invalid_path")
        self.assertNotIn(str(self.home), raw)
        self.rules.write_bytes(b"\xff")
        status, payload, raw = self.cli("--redact-paths")
        self.assertEqual(status, 2)
        self.assertEqual(payload["error"]["code"], "rules_error")
        self.assertNotIn(str(self.home), raw)


if __name__ == "__main__":
    unittest.main()
