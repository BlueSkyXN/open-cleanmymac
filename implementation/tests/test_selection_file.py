from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest import mock

from openclean import selection_file
from openclean.cleanup import SelectionError, _matches_selector, select_cleanup_items
from openclean.cli import main
from openclean.models import FileFacts, Item, ScanResult
from openclean.selection_file import SelectionFileError, load_selection_file


class SelectionFileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.enterContext(mock.patch.dict(os.environ, {"HOME": str(self.home)}))
        self.rules = self.home / "rules.json"
        self.rules.write_text('{"schema_version":1}')
        self.file = self.home / "selected.json"

    def write(self, values):
        self.file.write_text(json.dumps(values, ensure_ascii=False))
        return self.file

    def item(self, name="chosen", **kwargs):
        path = self.home / name
        path.mkdir()
        (path / "data").write_text("fixture")
        return Item(path, 4096, "fixture", domain="ai", identity=FileFacts.from_path(path).identity, **kwargs)

    def run_cli(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = main([*args, "--rules", str(self.rules), "--json"])
        return status, json.loads(stdout.getvalue())

    def test_load_deduplicates_preserving_exact_strings(self):
        values = [str(self.home / "a b"), str(self.home / "中文"), "docker:build-cache", str(self.home / "a b")]
        self.write(values)
        self.assertEqual(load_selection_file(self.file), tuple(values[:3]))

    def test_rejects_invalid_empty_or_oversized_input_without_content_leak(self):
        for values in ([], {}, "SECRET_CONTENT", [None], [True], [""], [" "], ["\x00"], ["x" * 4097]):
            with self.subTest(values_type=type(values).__name__):
                self.write(values)
                with self.assertRaises(SelectionFileError) as raised:
                    load_selection_file(self.file)
                self.assertNotIn("SECRET_CONTENT", str(raised.exception))
        self.file.write_text('["SECRET_BROKEN_JSON')
        with self.assertRaises(SelectionFileError) as raised:
            load_selection_file(self.file)
        self.assertNotIn("SECRET_BROKEN_JSON", str(raised.exception))
        self.write(["a", "b"])
        with mock.patch.object(selection_file, "MAX_SELECTORS", 1), self.assertRaises(SelectionFileError):
            load_selection_file(self.file)
        with mock.patch.object(selection_file, "MAX_SELECTION_BYTES", 2), self.assertRaises(SelectionFileError):
            load_selection_file(self.file)
        self.file.write_bytes(b"\xff")
        with self.assertRaises(SelectionFileError):
            load_selection_file(self.file)

    def test_special_files_and_changed_file_are_rejected(self):
        self.write(["a"])
        linked = self.home / "alias"
        linked.symlink_to(self.file)
        fifo = self.home / "fifo"
        os.mkfifo(fifo)
        for path in (linked, self.home, fifo, self.home / "missing"):
            with self.subTest(path=path.name), self.assertRaises(SelectionFileError):
                load_selection_file(path)
        stamp = selection_file._stamp
        calls = 0
        def changed(value):
            nonlocal calls
            calls += 1
            return (*stamp(value), calls)
        with mock.patch.object(selection_file, "_stamp", side_effect=changed), self.assertRaises(SelectionFileError):
            load_selection_file(self.file)

    def test_invalid_file_and_conflicts_fail_before_scan(self):
        self.write([])
        with mock.patch("openclean.cli.scan_domains", side_effect=AssertionError("must reject before scan")):
            code, result = self.run_cli(["clean", "ai", "--select-file", str(self.file)])
        self.assertEqual(code, 2)
        self.assertEqual(result["error"]["code"], "invalid_selection_file")
        self.write([str(self.home / "target")])
        conflicts = [["--all"], ["--select", "/tmp/explicit"], ["--force", "--yes"],
                     ["--run", "run:synthetic", "--finding", "finding:synthetic"], ["--interactive"]]
        for extra in conflicts:
            with self.subTest(extra=extra), \
                    mock.patch("openclean.cli.scan_domains", side_effect=AssertionError("must reject before scan")):
                code, _ = self.run_cli(["clean", "--select-file", str(self.file), *extra])
                self.assertEqual(code, 2)

    def test_exact_file_preview_never_inherits_default_selection(self):
        chosen = self.item(safety="critical", requires_explicit_selection=True, preselected=False)
        other = self.item("other", preselected=True)
        self.write([str(chosen.path)])
        with mock.patch("openclean.cli.scan_domains", return_value=ScanResult(items=[chosen, other])), \
                mock.patch("openclean.cli.execute_cleanup", side_effect=AssertionError("preview")):
            code, payload = self.run_cli(["clean", "ai", "--select-file", str(self.file), "--include-critical"])
        self.assertEqual(code, 0)
        selected = [item["path"] for category in payload["categories"] for item in category["items"] if item["preselected"]]
        self.assertEqual(selected, [str(chosen.path)])
        self.assertIsNone(payload["cleanup"])
        self.assertTrue(chosen.path.exists())
        self.assertTrue(other.path.exists())

    def test_file_selection_requires_risk_flag_and_executes_only_current_matches(self):
        chosen = self.item(safety="critical", requires_explicit_selection=True, preselected=False)
        other = self.item("other", preselected=True)
        self.write([str(chosen.path)])
        with mock.patch("openclean.cli.scan_domains", return_value=ScanResult(items=[chosen, other])):
            code, _ = self.run_cli(["clean", "ai", "--select-file", str(self.file), "--yes"])
            self.assertEqual(code, 2)
            self.assertTrue(chosen.path.exists())
            code, payload = self.run_cli(["clean", "ai", "--select-file", str(self.file), "--include-critical", "--yes"])
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["cleanup"]["outcomes"][0]["status"], "moved_to_trash")
        self.assertFalse(chosen.path.exists())
        self.assertTrue(other.path.exists())
        self.assertTrue((self.home / ".Trash/chosen/data").exists())

    def test_disappeared_blocked_or_glob_selector_cancels_whole_selection(self):
        chosen = self.item(preselected=True)
        blocked = replace(chosen, actionable=False, action_block_reason="changed state")
        for items, selectors in (([chosen], [str(chosen.path), str(self.home / "gone")]),
                                 ([blocked], [str(chosen.path)]), ([chosen], [str(self.home / "*")])):
            self.write(selectors)
            with mock.patch("openclean.cli.scan_domains", return_value=ScanResult(items=items)), \
                    mock.patch("openclean.cli.execute_cleanup", side_effect=AssertionError("invalid selection")):
                code, _ = self.run_cli(["clean", "ai", "--select-file", str(self.file), "--yes"])
            self.assertEqual(code, 2)
            self.assertTrue(chosen.path.exists())

    def test_tty_auto_mode_does_not_intercept_file_selection(self):
        chosen = self.item(preselected=False)
        self.write([str(chosen.path)])
        with mock.patch("sys.stdin.isatty", return_value=True), mock.patch("sys.stdout.isatty", return_value=True), \
                mock.patch("openclean.cli.scan_domains", return_value=ScanResult(items=[chosen])), \
                mock.patch("openclean.cli.start_scan_screen", side_effect=AssertionError("file selection must use text")), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = main(["clean", "ai", "--select-file", str(self.file), "--rules", str(self.rules)])
        self.assertEqual(code, 0)

    def test_purge_reuses_exact_selection_file(self):
        chosen = replace(self.item(), domain="project", project_root=self.home)
        self.write([str(chosen.path)])
        with mock.patch("openclean.cli.scan_project_artifacts", return_value=ScanResult(items=[chosen])), \
                mock.patch("openclean.cli.execute_cleanup", side_effect=AssertionError("preview")):
            code, _ = self.run_cli(["purge", str(self.home), "--select-file", str(self.file)])
        self.assertEqual(code, 0)

    def test_error_redaction_covers_paths_loaded_from_file(self):
        self.write(["/Volumes/PRIVATE-FROM-SELECTION-FILE/candidate"])
        with mock.patch("openclean.cli.scan_domains", return_value=ScanResult()):
            code, payload = self.run_cli(["clean", "ai", "--select-file", str(self.file), "--redact-paths"])
        self.assertEqual(code, 2)
        self.assertNotIn("PRIVATE-FROM-SELECTION-FILE", json.dumps(payload))

    def test_index_preserves_alias_ambiguity_and_diagnostic_semantics(self):
        chosen = self.item()
        for selector in (str(chosen.path), "~/chosen", str(self.home / "dummy/../chosen"), "not-found"):
            with self.subTest(selector=selector):
                expected = _matches_selector(chosen, selector)
                if expected:
                    self.assertEqual(select_cleanup_items([chosen], selectors=[selector]), [chosen])
                else:
                    with self.assertRaises(SelectionError):
                        select_cleanup_items([chosen], selectors=[selector])
        diagnostic = replace(chosen, actionable=False, diagnostic_kind="codex_home", resource_kind="filesystem_subset")
        self.assertEqual(select_cleanup_items([chosen, diagnostic], selectors=[str(chosen.path)]), [chosen])
        for candidates in ([chosen, chosen], [chosen, replace(chosen, category="another")]):
            with self.assertRaisesRegex(SelectionError, "不唯一"):
                select_cleanup_items(candidates, selectors=[str(chosen.path)])
        identifier = replace(chosen, identifier="fixture:one")
        self.assertEqual(select_cleanup_items([identifier], selectors=[identifier.identifier]), [identifier])

    def test_twenty_thousand_exact_targets_from_file(self):
        items = [Item(self.home / f"target-{index:05d}", 1, "fixture", safety="critical",
                      requires_explicit_selection=True, preselected=False) for index in range(20000)]
        self.write([str(item.path) for item in items])
        start = time.perf_counter()
        selectors = load_selection_file(self.file)
        selected = select_cleanup_items(items, selectors=selectors, include_critical=True)
        elapsed = time.perf_counter() - start
        self.assertEqual(selected, items)
        print(f"\nSelection file: 20000 exact targets loaded and selected in {elapsed:.3f}s")


if __name__ == "__main__":
    unittest.main()
