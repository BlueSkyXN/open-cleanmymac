from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from openclean.cleanup import execute_cleanup, select_cleanup_items
from openclean.cli import main
from openclean.engine import IgnoreRules, finalize_overlapping_result, scan_points
from openclean.processes import ProcessDetectionError, ProcessSnapshot
from openclean.scanpoints import AI_TOOL_JUNK, DOMAINS


class AIBrowserCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.gemini = self.home / ".gemini/antigravity-browser-profile"
        self.mcp = self.home / ".cache/chrome-devtools-mcp/chrome-profile"
        self.points = [replace(point, domain="ai") for point in AI_TOOL_JUNK
                       if point.category in {"Gemini 临时", "chrome-devtools-mcp"}]
        self.environment = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def cache(self, path: Path) -> Path:
        path.mkdir(parents=True)
        (path / "entry.bin").write_bytes(b"cache" * 1024)
        return path

    def scan(self, commands: tuple[str, ...] = (), ignore=None):
        with mock.patch("openclean.engine.capture_process_snapshot",
                        return_value=ProcessSnapshot(commands)):
            return scan_points(self.points, workers=1, ignore=ignore)

    def test_default_profile_caches_are_exact_and_not_preselected(self) -> None:
        names = ("Cache", "Code Cache", "GPUCache", "DawnGraphiteCache",
                 "DawnWebGPUCache", "Service Worker/CacheStorage")
        expected = {self.cache(self.gemini / "Default" / name) for name in names}
        expected.add(self.cache(self.gemini / "Cache"))
        for name in ("Default/IndexedDB", "Default/Local Storage", "Default/Sessions",
                     "Default/Cache-backup", "Profile 1/Cache", "Default/Plugins"):
            self.cache(self.gemini / name)
        for name in ("Default/Cookies", "Default/Login Data", "Local State"):
            path = self.gemini / name
            path.write_bytes(b"keep")
        result = self.scan()
        self.assertTrue(result.complete)
        self.assertEqual({item.path for item in result.items}, expected)
        self.assertTrue(all(item.actionable for item in result.items))
        self.assertTrue(all(not item.preselected for item in result.items))
        self.assertEqual(select_cleanup_items(result.items), [])
        finalized = finalize_overlapping_result(result)
        self.assertEqual(len(finalized.items), len(expected))
        self.assertEqual(finalized.total, result.total)

    def test_browser_process_remains_protected_after_agent_exit(self) -> None:
        self.cache(self.gemini / "Default/Cache")
        self.cache(self.mcp / "Default/Cache")
        commands = tuple(f'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome --user-data-dir={root}'
                         for root in (self.gemini, self.mcp))
        result = self.scan(commands)
        self.assertEqual(len(result.items), 2)
        self.assertTrue(all(not item.actionable for item in result.items))

    def test_legacy_dawn_cache_is_an_exact_optional_directory(self) -> None:
        expected = self.cache(self.mcp / "Default/DawnCache")
        self.cache(self.mcp / "Default/DawnCache-backup")
        self.cache(self.mcp / "Default/Downloads/DawnCache")
        result = self.scan()
        self.assertEqual([item.path for item in result.items], [expected])
        self.assertFalse(result.items[0].preselected)
        running = self.scan((f"Google Chrome --user-data-dir={self.mcp}",))
        self.assertFalse(running.items[0].actionable)

    def test_gr_shader_cache_belongs_to_user_data_root_not_default(self) -> None:
        expected = self.cache(self.mcp / "GrShaderCache")
        self.cache(self.mcp / "Default/GrShaderCache")
        self.cache(self.mcp / "GrShaderCache-backup")
        result = self.scan()
        self.assertEqual([item.path for item in result.items], [expected])
        self.assertFalse(result.items[0].preselected)
        running = self.scan((f"Google Chrome --user-data-dir={self.mcp}",))
        self.assertFalse(running.items[0].actionable)

    def test_process_detection_failure_blocks_both_profiles(self) -> None:
        self.cache(self.gemini / "Default/Cache")
        self.cache(self.mcp / "Default/Cache")
        with mock.patch("openclean.engine.capture_process_snapshot",
                        side_effect=ProcessDetectionError("unavailable")):
            result = scan_points(self.points, workers=1)
        self.assertEqual(len(result.items), 2)
        self.assertTrue(all(not item.actionable for item in result.items))

    def test_symlinked_default_and_ignored_cache_are_not_candidates(self) -> None:
        target = self.cache(self.home / "outside/Cache")
        self.gemini.mkdir(parents=True)
        (self.gemini / "Default").symlink_to(target.parent, target_is_directory=True)
        ignored = self.cache(self.mcp / "Default/Cache")
        result = self.scan(ignore=IgnoreRules([str(ignored)]))
        self.assertEqual(result.items, [])
        self.assertTrue((target / "entry.bin").exists())

    def test_exact_cleanup_preserves_profile_and_unselected_data(self) -> None:
        chosen = self.cache(self.gemini / "Default/Cache")
        retained = self.cache(self.gemini / "Default/Code Cache")
        cookies = self.gemini / "Default/Cookies"
        cookies.write_bytes(b"retain sign-in")
        result = self.scan()
        selected = select_cleanup_items(result.items, selectors=[str(chosen)])
        self.assertEqual([item.path for item in selected], [chosen])
        trash = self.home / ".Trash"
        trash.mkdir(mode=0o700)

        def rename(source_fd, source_name, destination_fd, destination_name):
            os.rename(source_name, destination_name, src_dir_fd=source_fd, dst_dir_fd=destination_fd)

        with mock.patch("openclean.cleanup._rename_no_replace", side_effect=rename):
            report = execute_cleanup(selected, IgnoreRules(), home=self.home,
                                     trash_resolver=lambda _: trash,
                                     process_runner=lambda *a, **kw: subprocess.CompletedProcess([], 0, "", ""))
        self.assertTrue(report.complete)
        self.assertFalse(chosen.exists())
        self.assertTrue(retained.exists())
        self.assertEqual(cookies.read_bytes(), b"retain sign-in")
        self.assertTrue((trash / "Cache/entry.bin").exists())

    def test_cli_json_preview_exposes_new_cache_without_mutation(self) -> None:
        chosen = self.cache(self.gemini / "Default/Cache")
        rules = self.home / "rules.json"
        rules.write_text('{"schema_version": 1}', encoding="utf-8")
        output = io.StringIO()
        with mock.patch.dict(DOMAINS, {"ai": self.points}), mock.patch(
            "openclean.engine.capture_process_snapshot", return_value=ProcessSnapshot(())
        ), contextlib.redirect_stdout(output):
            status = main(["clean", "ai", "--rules", str(rules), "--json"])
        payload = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertEqual(payload["schema_version"], 2)
        self.assertIn(str(chosen), output.getvalue())
        self.assertIsNone(payload["cleanup"])
        self.assertTrue((chosen / "entry.bin").exists())


if __name__ == "__main__":
    unittest.main()
