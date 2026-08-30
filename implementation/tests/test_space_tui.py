from __future__ import annotations

import curses
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openclean.analyzer import analyze_path
from openclean.engine import IgnoreRules
from openclean.models import Item
from openclean.space_tui import (
    SpaceTUIUnavailable,
    _run_space_review,
    _toggle_item,
    review_space,
)


class _FakeScreen:
    def __init__(self, keys: list[int], height: int = 24, width: int = 120):
        self.keys = list(keys)
        self.height = height
        self.width = width
        self.lines: list[str] = []

    def keypad(self, enabled: bool) -> None:
        self.keypad_enabled = enabled

    def getmaxyx(self) -> tuple[int, int]:
        return self.height, self.width

    def erase(self) -> None:
        self.lines.append("<erase>")

    def addnstr(self, _row: int, _column: int, text: str, _length: int) -> None:
        self.lines.append(text)

    def refresh(self) -> None:
        pass

    def getch(self) -> int:
        if not self.keys:
            raise AssertionError("测试按键已耗尽")
        return self.keys.pop(0)


class SpaceTuiTests(unittest.TestCase):
    def _run(
        self,
        root: Path,
        keys: list[int],
        *,
        allow_execution: bool,
        top: int = 0,
    ):
        screen = _FakeScreen(keys)
        with mock.patch.dict(os.environ, {"HOME": str(root.parent)}), mock.patch(
            "curses.curs_set"
        ):
            result = _run_space_review(
                screen,
                root,
                protection=IgnoreRules(),
                top=top,
                allow_execution=allow_execution,
            )
        return result, screen

    def test_top_limit_is_visible_in_browser_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            root.mkdir()
            (root / "large.bin").write_bytes(b"x" * 10_000)
            (root / "small.bin").write_bytes(b"x")

            _, screen = self._run(
                root,
                [ord("q")],
                allow_execution=False,
                top=1,
            )

            self.assertTrue(
                any("显示最大 1/2 项" in line for line in screen.lines)
            )
            self.assertTrue(any("占比基于全部" in line for line in screen.lines))

    def test_navigation_selection_deduplicates_descendants(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            directory = root / "large"
            directory.mkdir(parents=True)
            nested = directory / "nested.bin"
            nested.write_bytes(b"n" * 20_000)
            (root / "small.bin").write_bytes(b"small")

            result, _ = self._run(
                root,
                [
                    curses.KEY_RIGHT,
                    ord(" "),
                    curses.KEY_LEFT,
                    ord(" "),
                    ord("d"),
                    10,
                ],
                allow_execution=False,
            )

            self.assertTrue(result.submitted)
            self.assertFalse(result.execution_confirmed)
            self.assertEqual(len(result.selected), 1)
            self.assertEqual(result.selected[0].path, directory)
            self.assertTrue(nested.exists())

    def test_yes_requires_critical_bang_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            root.mkdir()
            selected = root / "file.bin"
            selected.write_bytes(b"data")

            result, screen = self._run(
                root,
                [ord(" "), curses.KEY_DC, ord("y"), ord("!")],
                allow_execution=True,
            )

            self.assertTrue(result.execution_confirmed)
            self.assertEqual(result.selected[0].path, selected)
            self.assertTrue(any("同卷 Trash" in line for line in screen.lines))
            self.assertTrue(selected.exists())

    def test_reveal_and_cancel_are_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            root.mkdir()
            selected = root / "file.bin"
            selected.write_bytes(b"data")
            revealed: list[Path] = []
            screen = _FakeScreen([ord("o"), ord("q")])

            with mock.patch("curses.curs_set"):
                result = _run_space_review(
                    screen,
                    root,
                    protection=IgnoreRules(),
                    top=0,
                    allow_execution=False,
                    revealer=revealed.append,
                )

            self.assertTrue(result.cancelled)
            self.assertEqual(revealed, [selected])
            self.assertEqual(selected.read_bytes(), b"data")

    def test_selected_parent_blocks_duplicate_child_selection(self) -> None:
        parent = Item(
            path=Path("/Users/example/project"),
            size=100,
            category="parent",
        )
        child = Item(
            path=Path("/Users/example/project/file"),
            size=50,
            category="child",
        )
        selected: dict[Path, Item] = {}

        _toggle_item(parent, selected)
        message = _toggle_item(child, selected)

        self.assertEqual(list(selected), [parent.path])
        self.assertIn("上级目录", message)

    def test_wrapper_reports_curses_failure(self) -> None:
        with mock.patch(
            "curses.wrapper", side_effect=curses.error("no terminal")
        ), self.assertRaisesRegex(SpaceTUIUnavailable, "无法启动"):
            review_space(
                Path("/"),
                protection=IgnoreRules(),
                allow_execution=False,
            )

    def test_cursor_moves_do_not_rescan_current_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            root.mkdir()
            (root / "one.bin").write_bytes(b"one")
            (root / "two.bin").write_bytes(b"two")
            analysis = analyze_path(root)
            analyzer = mock.Mock(return_value=analysis)
            screen = _FakeScreen(
                [curses.KEY_DOWN, curses.KEY_UP, ord("q")]
            )

            with mock.patch("curses.curs_set"):
                _run_space_review(
                    screen,
                    root,
                    protection=IgnoreRules(),
                    top=0,
                    allow_execution=False,
                    analyzer=analyzer,
                )

            analyzer.assert_called_once()


if __name__ == "__main__":
    unittest.main()
