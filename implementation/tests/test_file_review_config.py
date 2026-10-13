from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openclean.cli import main
from openclean.config import CliConfig, ConfigError, ConfigStore
from openclean.engine import Control, DOMAINS
from openclean.scanpoints import ScanPoint
from openclean.scan_tui import ScanScreenOutcome
from openclean.tui import ReviewResult


def invoke(argv):
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
        status = main([*argv, "--json"])
    return status, json.loads(stdout.getvalue())


class FileReviewConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        self.first = self.home / "saved downloads"
        self.second = self.home / "second"
        self.default = self.home / "Downloads"
        for root in (self.first, self.second, self.default):
            root.mkdir()
            (root / "document.txt").write_text(root.name)
        self.config = self.home / ".config/openclean/config.json"
        self.store = ConfigStore(self.config)
        self.enterContext(mock.patch.dict(os.environ, {"HOME": str(self.home)}))
        self.enterContext(mock.patch.dict(DOMAINS, {
            "system": [ScanPoint("文件审阅", ("~/Downloads",), "critical", scanner="downloads")],
            "developer": [],
        }))

    def test_round_trip_preserves_other_preferences_and_reset_restores_old_shape(self):
        self.store.set_analytics(True)
        saved = self.store.set_file_roots([self.first, self.second, self.first])
        self.assertTrue(saved.analytics_enabled)
        self.assertEqual(saved.file_roots, (str(self.first), str(self.second)))
        self.assertEqual(self.store.load(), saved)
        before = self.config.stat().st_mtime_ns
        self.store.set_file_roots([self.first, self.second])
        self.assertEqual(self.config.stat().st_mtime_ns, before)
        self.store.set_analytics(False)
        self.assertEqual(self.store.load().file_roots, saved.file_roots)
        self.store.set_file_roots([])
        self.assertEqual(json.loads(self.config.read_text()), {
            "schema_version": 1, "analytics_enabled": False,
        })
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)

    def test_invalid_root_or_ancestor_link_never_overwrites_config(self):
        self.store.set_file_roots([self.first])
        before = self.config.read_bytes()
        alias = self.home / "alias"
        alias.symlink_to(self.home, target_is_directory=True)
        for roots in ([self.second, self.home / "missing"], [self.first / "document.txt"],
                      [alias / self.second.name], [""], ["\0"]):
            with self.subTest(roots=roots), self.assertRaises(ConfigError):
                self.store.set_file_roots(roots)
            self.assertEqual(self.config.read_bytes(), before)

    def test_persisted_config_requires_absolute_bounded_paths(self):
        for roots in ("/tmp", ["relative"], [3], ["/bad\0"], [str(self.first)] * 65):
            with self.subTest(roots=roots), self.assertRaises(ConfigError):
                CliConfig.from_mapping({"file_roots": roots})
        self.assertEqual(CliConfig.from_mapping({"analytics_enabled": True}).file_roots, ())
        self.assertEqual(CliConfig.from_mapping({"file_roots": ["/offline"]}).file_roots, ("/offline",))

    def test_config_command_is_replace_not_append_and_does_not_scan(self):
        with mock.patch("openclean.cli.scan_domains") as scan:
            status, payload = invoke(["config", "--file-root", str(self.first),
                                      "--file-root", str(self.second)])
            self.assertEqual(status, 0)
            self.assertTrue(payload["changed"])
            invoke(["config", "--file-root", str(self.second)])
            self.assertEqual(self.store.load().file_roots, (str(self.second),))
            status, payload = invoke(["config", "--clear-file-roots"])
            self.assertEqual(status, 0)
            self.assertEqual(payload["effective_file_roots"], [str(self.default)])
            scan.assert_not_called()

    def test_scan_and_clean_reuse_saved_roots_as_read_only(self):
        self.store.set_file_roots([self.first])
        for argv in (["scan", "--domain", "system"], ["clean", "junk"]):
            with self.subTest(argv=argv):
                status, payload = invoke(argv)
                self.assertEqual(status, 0)
                scope = next(d for d in payload["diagnostics"] if d["kind"] == "file_review_scope")
                self.assertEqual(scope["source"], "config")
                self.assertEqual(scope["roots"], [str(self.first)])
                files = [f for d in payload["diagnostics"] for f in d.get("files", [])]
                self.assertEqual([f["path"] for f in files], [str(self.first / "document.txt")])
                self.assertEqual(payload["reclaimable_bytes"], 0)
                self.assertEqual(payload["potential_bytes"], 0)
        with mock.patch("openclean.cli.execute_cleanup") as execute:
            status, payload = invoke(["clean", "junk", "--select", str(self.first),
                                      "--include-critical", "--yes", "--no-interactive"])
            self.assertEqual(status, 2)
            execute.assert_not_called()
        self.assertTrue((self.first / "document.txt").exists())

    def test_explicit_root_overrides_saved_roots_and_does_not_rewrite_config(self):
        self.store.set_file_roots([self.first])
        before = self.config.read_bytes()
        status, payload = invoke(["scan", "--domain", "system", "--file-root", str(self.second)])
        self.assertEqual(status, 0)
        scope = payload["diagnostics"][0]
        self.assertEqual(scope["source"], "argument")
        self.assertEqual(scope["roots"], [str(self.second)])
        self.assertEqual(self.config.read_bytes(), before)

    def test_offline_saved_root_is_partial_not_empty_or_default_fallback(self):
        self.store.set_file_roots([self.first, self.second])
        (self.first / "document.txt").unlink()
        self.first.rmdir()
        status, payload = invoke(["scan", "--domain", "system"])
        self.assertEqual(status, 1)
        self.assertFalse(payload["complete"])
        self.assertFalse(payload["diagnostics"][0]["complete"])
        self.assertTrue(any(i["code"] == "file_review_root_unavailable" and i["blocking"]
                            for i in payload["issues"]))
        files = [f for d in payload["diagnostics"] for f in d.get("files", [])]
        self.assertEqual([f["path"] for f in files], [str(self.second / "document.txt")])
        self.assertEqual(len(self.store.load().file_roots), 2)
        self.assertEqual(invoke(["config", "--clear-file-roots"])[0], 0)

    def test_nested_roots_do_not_duplicate_file_measurements(self):
        child = self.first / "nested"
        child.mkdir()
        (child / "nested.txt").write_text("fixture")
        self.store.set_file_roots([self.first, child])
        status, payload = invoke(["scan", "--domain", "system"])
        self.assertEqual(status, 0)
        paths = [f["path"] for d in payload["diagnostics"] for f in d.get("files", [])]
        self.assertEqual(len(paths), 2)
        self.assertEqual(len(set(paths)), 2)

    def test_invalid_config_blocks_system_scan_but_not_other_domains_or_explicit_scope(self):
        self.config.parent.mkdir(parents=True)
        self.config.write_text("invalid")
        with mock.patch("openclean.cli.scan_domains") as scan:
            status, payload = invoke(["scan", "--domain", "system"])
            self.assertEqual(status, 2)
            self.assertEqual(payload["error"]["code"], "config_error")
            scan.assert_not_called()
        self.assertEqual(invoke(["scan", "--domain", "developer"])[0], 0)
        self.assertEqual(invoke(["scan", "--domain", "system", "--file-root", str(self.second)])[0], 0)
        self.assertEqual(self.config.read_text(), "invalid")

    def test_custom_config_path_and_root_paths_are_redacted(self):
        custom = self.home / "alternate.json"
        ConfigStore(custom).set_file_roots([self.first])
        for argv in (["config", "--config-path", str(custom)],
                     ["scan", "--domain", "system", "--config-path", str(custom)],
                     ["clean", "junk", "--config-path", str(custom)]):
            status, payload = invoke([*argv, "--redact-paths"])
            self.assertEqual(status, 0)
            self.assertNotIn(str(self.home), json.dumps(payload))

    def test_clean_tui_uses_saved_scope(self):
        self.store.set_file_roots([self.first])
        captured = {}

        def scan_screen(action, **_options):
            result = action(Control(), lambda _progress: None)
            captured["files"] = [f for d in result.diagnostics for f in d.get("files", [])]
            return ScanScreenOutcome("done", result=result)

        def review(groups, **options):
            self.assertEqual(str(groups[0].items[0].path), str(self.first))
            self.assertEqual([f["path"] for f in captured["files"]], [str(self.first / "document.txt")])
            return ReviewResult((), True, False, False)

        with mock.patch.object(sys.stdin, "isatty", return_value=True), \
                mock.patch.object(sys.stdout, "isatty", return_value=True), \
                mock.patch("openclean.cli.start_scan_screen", side_effect=scan_screen) as scan, \
                mock.patch("openclean.cli.review_cleanup", side_effect=review) as display, \
                mock.patch("builtins.print"):
            self.assertEqual(main(["clean", "junk", "--interactive"]), 0)
        scan.assert_called_once()
        display.assert_called_once()

    def test_agent_mode_rejects_classic_review_configuration(self):
        for options in (["--config-path", str(self.config)],):
            with mock.patch("openclean.cli._cmd_clean_finding") as agent:
                status, payload = invoke(["clean", "--run", "run:fixture", "--finding", "finding:fixture", *options])
            self.assertEqual(status, 2)
            self.assertEqual(payload["error"]["code"], "mixed_clean_modes")
            agent.assert_not_called()


if __name__ == "__main__":
    unittest.main()
