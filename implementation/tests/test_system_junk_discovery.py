from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openclean.cleanup import execute_cleanup
from openclean.engine import IgnoreRules, scan_points
from openclean.macos import (
    DarwinUserCacheDiscovery,
    discover_darwin_user_cache,
)
from openclean.models import ScanIssue
from openclean.scanpoints import ScanPoint


class DarwinUserCacheDiscoveryTests(unittest.TestCase):
    def test_getconf_success_is_normalized(self) -> None:
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(
                command,
                0,
                "/var/folders/ab/current-user/C/\n",
                "",
            )

        discovery = discover_darwin_user_cache(runner=runner, timeout=1.5)

        self.assertEqual(
            discovery.paths,
            (Path("/var/folders/ab/current-user/C"),),
        )
        self.assertEqual(discovery.issues, ())
        self.assertEqual(
            calls[0][0],
            ["/usr/bin/getconf", "DARWIN_USER_CACHE_DIR"],
        )
        self.assertEqual(calls[0][1]["timeout"], 1.5)
        self.assertFalse(calls[0][1]["check"])

    def test_getconf_failure_and_invalid_output_are_explicit(self) -> None:
        failed = discover_darwin_user_cache(
            runner=lambda command, **_: subprocess.CompletedProcess(
                command,
                1,
                "",
                "not available",
            )
        )
        relative = discover_darwin_user_cache(
            runner=lambda command, **_: subprocess.CompletedProcess(
                command,
                0,
                "relative/cache\n",
                "",
            )
        )

        self.assertEqual(failed.paths, ())
        self.assertEqual(failed.issues[0].code, "path_discovery_failed")
        self.assertIn("not available", failed.issues[0].message)
        self.assertEqual(relative.paths, ())
        self.assertIn("不是绝对路径", relative.issues[0].message)

    def test_getconf_timeout_is_explicit(self) -> None:
        def timeout(command, **_):
            raise subprocess.TimeoutExpired(command, 0.25)

        discovery = discover_darwin_user_cache(
            runner=timeout,
            timeout=0.25,
        )

        self.assertEqual(discovery.paths, ())
        self.assertIn("0.25 秒", discovery.issues[0].message)


class DarwinUserCacheScanTests(unittest.TestCase):
    def _point(self) -> ScanPoint:
        return ScanPoint(
            "Darwin 用户缓存",
            (),
            "confirm",
            expand_children=True,
            path_provider="darwin-user-cache",
        )

    def test_provider_scans_only_direct_children_and_records_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_root = Path(tmp) / "C"
            first = cache_root / "first"
            second = cache_root / "second.bin"
            first.mkdir(parents=True)
            (first / "nested.bin").write_bytes(b"nested")
            second.write_bytes(b"second")
            discovery = DarwinUserCacheDiscovery(paths=(cache_root,))

            with mock.patch(
                "openclean.engine.discover_darwin_user_cache",
                return_value=discovery,
            ):
                result = scan_points([self._point()], workers=1)

            self.assertTrue(result.complete)
            self.assertEqual(
                {item.path for item in result.items},
                {first, second},
            )
            self.assertTrue(
                all(item.cleanup_scope == "darwin-user-cache" for item in result.items)
            )
            self.assertTrue(
                all(item.cleanup_root == cache_root for item in result.items)
            )
            self.assertTrue(
                all(item.cleanup_root_identity is not None for item in result.items)
            )
            self.assertTrue(all(item.preselected is False for item in result.items))

    def test_provider_failure_makes_scan_incomplete(self) -> None:
        discovery = DarwinUserCacheDiscovery(
            issues=(
                ScanIssue(
                    code="path_discovery_failed",
                    message="unavailable",
                    task="Darwin 用户缓存",
                ),
            )
        )

        with mock.patch(
            "openclean.engine.discover_darwin_user_cache",
            return_value=discovery,
        ):
            result = scan_points([self._point()], workers=1)

        self.assertFalse(result.complete)
        self.assertEqual(result.items, [])
        self.assertEqual(result.issues[0].code, "path_discovery_failed")

    def test_unreadable_descendant_blocks_partial_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_root = Path(tmp) / "C"
            candidate = cache_root / "candidate"
            unreadable = candidate / "unreadable"
            unreadable.mkdir(parents=True)
            (candidate / "visible.bin").write_bytes(b"visible")
            discovery = DarwinUserCacheDiscovery(paths=(cache_root,))
            real_scandir = os.scandir

            def guarded_scandir(path):
                if Path(path) == unreadable:
                    raise PermissionError("denied")
                return real_scandir(path)

            with mock.patch(
                "openclean.engine.discover_darwin_user_cache",
                return_value=discovery,
            ), mock.patch(
                "openclean.engine.os.scandir",
                side_effect=guarded_scandir,
            ):
                result = scan_points([self._point()], workers=1)

            self.assertFalse(result.complete)
            self.assertEqual(len(result.items), 1)
            self.assertFalse(result.items[0].actionable)
            self.assertEqual(result.items[0].excluded_paths, 1)
            self.assertIn("忽略或保护路径", result.items[0].action_block_reason)

    def test_uninspectable_file_blocks_partial_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_root = Path(tmp) / "C"
            candidate = cache_root / "candidate"
            denied = candidate / "denied.bin"
            candidate.mkdir(parents=True)
            (candidate / "visible.bin").write_bytes(b"visible")
            denied.write_bytes(b"denied")
            discovery = DarwinUserCacheDiscovery(paths=(cache_root,))
            original_lstat = Path.lstat

            def guarded_lstat(path: Path):
                if path == denied:
                    raise PermissionError("denied")
                return original_lstat(path)

            with mock.patch(
                "openclean.engine.discover_darwin_user_cache",
                return_value=discovery,
            ), mock.patch.object(
                Path,
                "lstat",
                new=guarded_lstat,
            ):
                result = scan_points([self._point()], workers=1)

            self.assertFalse(result.complete)
            self.assertEqual(len(result.items), 1)
            self.assertFalse(result.items[0].actionable)
            self.assertEqual(result.items[0].excluded_paths, 1)

    def test_valid_live_scope_can_move_one_child_without_removing_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / "home"
            cache_root = base / "C"
            selected = cache_root / "selected"
            sibling = cache_root / "sibling"
            trash = home / ".Trash"
            home.mkdir()
            selected.mkdir(parents=True)
            sibling.mkdir()
            trash.mkdir()
            (selected / "data.bin").write_bytes(b"selected")
            (sibling / "data.bin").write_bytes(b"sibling")
            discovery = DarwinUserCacheDiscovery(paths=(cache_root,))

            with mock.patch(
                "openclean.engine.discover_darwin_user_cache",
                return_value=discovery,
            ):
                item = scan_points([self._point()], workers=1).items[0]
            with mock.patch(
                "openclean.cleanup.discover_darwin_user_cache",
                return_value=discovery,
            ), mock.patch(
                "openclean.cleanup.nonprivileged_action_block_reason",
                return_value="系统保护路径",
            ):
                report = execute_cleanup(
                    [item],
                    IgnoreRules(),
                    home=home,
                    trash_resolver=lambda _: trash,
                )

            self.assertTrue(report.complete)
            self.assertTrue(cache_root.is_dir())
            self.assertTrue(sibling.is_dir())
            self.assertFalse(selected.exists())
            self.assertTrue((trash / "selected").is_dir())

    def test_changed_live_scope_blocks_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / "home"
            cache_root = base / "C"
            child = cache_root / "child"
            home.mkdir()
            child.mkdir(parents=True)
            (child / "data.bin").write_bytes(b"data")
            scanned = DarwinUserCacheDiscovery(paths=(cache_root,))

            with mock.patch(
                "openclean.engine.discover_darwin_user_cache",
                return_value=scanned,
            ):
                item = scan_points([self._point()], workers=1).items[0]
            changed = DarwinUserCacheDiscovery(paths=(base / "other-C",))
            with mock.patch(
                "openclean.cleanup.discover_darwin_user_cache",
                return_value=changed,
            ):
                report = execute_cleanup([item], IgnoreRules(), home=home)

            self.assertFalse(report.complete)
            self.assertEqual(report.outcomes[0].status, "blocked")
            self.assertIn("根路径已变化", report.outcomes[0].message)
            self.assertTrue(child.is_dir())
            self.assertFalse((home / ".Trash").exists())


if __name__ == "__main__":
    unittest.main()
