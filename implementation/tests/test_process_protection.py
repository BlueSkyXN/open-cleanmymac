from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openclean.cleanup import execute_cleanup
from openclean.engine import IgnoreRules, scan_points
from openclean.processes import (
    ProcessDetectionError,
    ProcessSnapshot,
    capture_process_snapshot,
)
from openclean.scanpoints import AI_TOOL_JUNK, SYSTEM_JUNK, ScanPoint


class ProcessSnapshotTests(unittest.TestCase):
    def test_capture_and_case_insensitive_marker_matching(self) -> None:
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(
                command,
                0,
                "/Applications/Xcode.app/Contents/MacOS/Xcode\n"
                "/usr/bin/python worker.py\n",
                "",
            )

        snapshot = capture_process_snapshot(runner=runner)

        self.assertEqual(calls[0][0], ["/bin/ps", "-axo", "command="])
        self.assertTrue(snapshot.any_running(("xcode.app",)))
        self.assertFalse(snapshot.any_running(("codex",)))
        self.assertEqual(len(snapshot.commands), 2)

    def test_capture_failure_and_timeout_are_explicit(self) -> None:
        with self.assertRaisesRegex(ProcessDetectionError, "ps failed"):
            capture_process_snapshot(
                runner=lambda command, **_: subprocess.CompletedProcess(
                    command, 1, "", "ps failed"
                )
            )

        def timeout(command, **_):
            raise subprocess.TimeoutExpired(command, 0.01)

        with self.assertRaisesRegex(ProcessDetectionError, "0.01 秒"):
            capture_process_snapshot(runner=timeout, timeout=0.01)


class ProcessProtectedScanTests(unittest.TestCase):
    def test_running_tool_skips_scan_point_as_nonblocking_information(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache"
            cache.mkdir()
            (cache / "data.bin").write_bytes(b"data")
            point = ScanPoint(
                "Codex 缓存",
                (str(cache),),
                running_process_markers=("codex",),
            )

            with mock.patch(
                "openclean.engine.capture_process_snapshot",
                return_value=ProcessSnapshot(("/Applications/Codex.app/Codex",)),
            ):
                result = scan_points([point], workers=1)

            self.assertTrue(result.complete)
            self.assertEqual(result.items, [])
            self.assertEqual(result.issues[0].code, "resource_in_use")
            self.assertFalse(result.issues[0].blocking)

    def test_process_detection_failure_blocks_only_protected_scan_points(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            protected = root / "protected"
            ordinary = root / "ordinary"
            protected.mkdir()
            ordinary.mkdir()
            (protected / "a.bin").write_bytes(b"a")
            (ordinary / "b.bin").write_bytes(b"b")
            points = [
                ScanPoint(
                    "Protected",
                    (str(protected),),
                    running_process_markers=("tool",),
                ),
                ScanPoint("Ordinary", (str(ordinary),)),
            ]

            with mock.patch(
                "openclean.engine.capture_process_snapshot",
                side_effect=ProcessDetectionError("ps unavailable"),
            ):
                result = scan_points(points, workers=1)

            self.assertFalse(result.complete)
            self.assertEqual([item.category for item in result.items], ["Ordinary"])
            self.assertEqual(result.issues[0].code, "process_detection_failed")
            self.assertTrue(result.issues[0].blocking)

    def test_public_xcode_and_ai_points_are_precise_and_process_guarded(self) -> None:
        system = {point.category: point for point in SYSTEM_JUNK}
        ai = {point.category: point for point in AI_TOOL_JUNK}

        self.assertIn("Xcode 文档缓存", system)
        self.assertIn("Xcode 设备日志", system)
        self.assertEqual(system["Xcode Archives"].safety, "critical")
        self.assertTrue(system["Xcode DerivedData"].running_process_markers)
        self.assertNotIn("~/.codex/cache", ai["Codex 缓存"].paths)
        self.assertIn(
            "~/.codex/cache/codex_apps_tools",
            ai["Codex 缓存"].paths,
        )
        self.assertIn(
            "~/.claude/stats-cache.json",
            ai["Claude 缓存"].paths,
        )
        self.assertTrue(ai["chrome-devtools-mcp"].running_process_markers)


class ProcessProtectedExecutionTests(unittest.TestCase):
    def _scanned_item(self, cache: Path):
        point = ScanPoint(
            "Tool cache",
            (str(cache),),
            running_process_markers=("tool-app",),
        )
        with mock.patch(
            "openclean.engine.capture_process_snapshot",
            return_value=ProcessSnapshot(()),
        ):
            return scan_points([point], workers=1).items[0]

    def test_tool_starting_after_preflight_blocks_final_move(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            cache = home / "cache"
            cache.mkdir(parents=True)
            (cache / "data.bin").write_bytes(b"data")
            item = self._scanned_item(cache)
            calls = 0

            def runner(command, **_):
                nonlocal calls
                calls += 1
                output = "" if calls == 1 else "/Applications/Tool-App"
                return subprocess.CompletedProcess(command, 0, output, "")

            report = execute_cleanup(
                [item],
                IgnoreRules(),
                home=home,
                process_runner=runner,
            )

            self.assertFalse(report.complete)
            self.assertEqual(report.outcomes[0].status, "failed")
            self.assertIn("已启动", report.outcomes[0].message)
            self.assertTrue(cache.exists())
            self.assertFalse((home / ".Trash").exists())

    def test_detection_failure_blocks_batch_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            cache = home / "cache"
            cache.mkdir(parents=True)
            (cache / "data.bin").write_bytes(b"data")
            item = self._scanned_item(cache)

            def runner(command, **_):
                return subprocess.CompletedProcess(command, 1, "", "denied")

            report = execute_cleanup(
                [item],
                IgnoreRules(),
                home=home,
                process_runner=runner,
            )

            self.assertFalse(report.complete)
            self.assertEqual(report.outcomes[0].status, "blocked")
            self.assertIn("进程检测失败", report.outcomes[0].message)
            self.assertTrue(cache.exists())

    def test_stopped_tool_allows_move_after_three_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            cache = home / "cache"
            cache.mkdir(parents=True)
            (cache / "data.bin").write_bytes(b"data")
            item = self._scanned_item(cache)
            runner = mock.Mock(
                side_effect=lambda command, **_: subprocess.CompletedProcess(
                    command, 0, "/usr/bin/other-process", ""
                )
            )

            report = execute_cleanup(
                [item],
                IgnoreRules(),
                home=home,
                process_runner=runner,
            )

            self.assertTrue(report.complete)
            self.assertEqual(report.outcomes[0].status, "moved_to_trash")
            self.assertEqual(runner.call_count, 3)
            self.assertFalse(cache.exists())


if __name__ == "__main__":
    unittest.main()
