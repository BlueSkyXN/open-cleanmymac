from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openclean.analyzer import analyze_path
from openclean.cli import main
from openclean.space_tui import SpaceReviewResult, SpaceTUIUnavailable


class _TTYBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


def _rules(home: Path) -> Path:
    path = home / "rules.json"
    path.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    return path


class AnalyzeCleanupTests(unittest.TestCase):
    def test_non_tty_exact_selection_preview_never_writes_without_yes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = home / "data"
            root.mkdir(parents=True)
            selected = root / "selected.bin"
            selected.write_bytes(b"data")
            rules = _rules(home)
            stdout = io.StringIO()

            with mock.patch.dict(os.environ, {"HOME": str(home)}), \
                    contextlib.redirect_stdout(stdout):
                status = main(
                    [
                        "analyze",
                        str(root),
                        "--select",
                        str(selected),
                        "--rules",
                        str(rules),
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(status, 0)
            self.assertEqual(payload["mode"], "selection-preview")
            self.assertEqual(payload["selection"]["paths"], [str(selected)])
            self.assertIsNone(payload["cleanup"])
            self.assertTrue(selected.exists())
            self.assertFalse((home / ".Trash").exists())

    def test_non_tty_yes_moves_exact_current_level_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = home / "data"
            root.mkdir(parents=True)
            selected = root / "selected.bin"
            selected.write_bytes(b"data")
            rules = _rules(home)
            stdout = io.StringIO()

            with mock.patch.dict(os.environ, {"HOME": str(home)}), \
                    contextlib.redirect_stdout(stdout):
                status = main(
                    [
                        "analyze",
                        str(root),
                        "--select",
                        str(selected),
                        "--yes",
                        "--rules",
                        str(rules),
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(status, 0)
            self.assertEqual(payload["mode"], "result")
            self.assertEqual(
                payload["cleanup"]["outcomes"][0]["status"],
                "moved_to_trash",
            )
            self.assertFalse(selected.exists())
            self.assertTrue((home / ".Trash" / "selected.bin").exists())

    def test_non_tty_yes_requires_exact_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = home / "data"
            root.mkdir(parents=True)
            selected = root / "selected.bin"
            selected.write_bytes(b"data")
            rules = _rules(home)
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                status = main(
                    ["analyze", str(root), "--yes", "--rules", str(rules)]
                )

            self.assertEqual(status, 2)
            self.assertIn("--select", stderr.getvalue())
            self.assertTrue(selected.exists())

    def test_selector_must_be_an_exact_current_level_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = home / "data"
            nested = root / "directory"
            nested.mkdir(parents=True)
            child = nested / "child.bin"
            child.write_bytes(b"data")
            rules = _rules(home)
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                status = main(
                    [
                        "analyze",
                        str(root),
                        "--select",
                        str(child),
                        "--rules",
                        str(rules),
                    ]
                )

            self.assertEqual(status, 2)
            self.assertIn("当前层级未找到", stderr.getvalue())
            self.assertTrue(child.exists())

    def test_tty_selection_needs_yes_and_tui_confirmation_to_execute(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = home / "data"
            root.mkdir(parents=True)
            selected = root / "selected.bin"
            selected.write_bytes(b"data")
            rules = _rules(home)
            stdin = _TTYBuffer()

            def review_without_execution(path, **_):
                item = analyze_path(path).entries[0].item
                return SpaceReviewResult((item,), True, False, False)

            with mock.patch.dict(os.environ, {"HOME": str(home)}), mock.patch.object(
                sys, "stdin", stdin
            ), contextlib.redirect_stdout(_TTYBuffer()), mock.patch(
                "openclean.cli.review_space", side_effect=review_without_execution
            ):
                preview = main(
                    ["analyze", str(root), "--rules", str(rules)]
                )

            self.assertEqual(preview, 0)
            self.assertTrue(selected.exists())
            self.assertFalse((home / ".Trash").exists())

            def review_and_confirm(path, **_):
                item = analyze_path(path).entries[0].item
                return SpaceReviewResult((item,), True, True, False)

            with mock.patch.dict(os.environ, {"HOME": str(home)}), mock.patch.object(
                sys, "stdin", stdin
            ), contextlib.redirect_stdout(_TTYBuffer()), mock.patch(
                "openclean.cli.review_space", side_effect=review_and_confirm
            ):
                executed = main(
                    [
                        "analyze",
                        str(root),
                        "--yes",
                        "--rules",
                        str(rules),
                    ]
                )

            self.assertEqual(executed, 0)
            self.assertFalse(selected.exists())
            self.assertTrue((home / ".Trash" / "selected.bin").exists())

    def test_tui_failure_with_yes_never_falls_through_to_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = home / "data"
            root.mkdir(parents=True)
            selected = root / "selected.bin"
            selected.write_bytes(b"data")
            rules = _rules(home)
            stderr = io.StringIO()

            with mock.patch.object(sys, "stdin", _TTYBuffer()), \
                    contextlib.redirect_stdout(_TTYBuffer()), \
                    contextlib.redirect_stderr(stderr), mock.patch(
                        "openclean.cli.review_space",
                        side_effect=SpaceTUIUnavailable("no curses"),
                    ):
                status = main(
                    [
                        "analyze",
                        str(root),
                        "--yes",
                        "--rules",
                        str(rules),
                    ]
                )

            self.assertEqual(status, 2)
            self.assertIn("no curses", stderr.getvalue())
            self.assertTrue(selected.exists())


if __name__ == "__main__":
    unittest.main()
