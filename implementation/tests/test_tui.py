from __future__ import annotations

import curses
import unittest
from pathlib import Path
from unittest import mock

from openclean.models import Item
from openclean.tui import (
    ReviewGroup,
    TUIUnavailable,
    _run_review,
    review_cleanup,
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


def _item(
    name: str,
    safety: str,
    *,
    preselected: bool,
    actionable: bool = True,
    requires_explicit_selection: bool = False,
) -> Item:
    return Item(
        path=Path(f"/Users/example/{name}"),
        size=100,
        category=name,
        safety=safety,
        preselected=preselected,
        domain="developer",
        actionable=actionable,
        requires_explicit_selection=requires_explicit_selection,
        action_block_reason=("blocked" if not actionable else ""),
    )


class ReviewTuiStateTests(unittest.TestCase):
    def _run(self, groups, keys, *, allow_execution):
        screen = _FakeScreen(keys)
        with mock.patch("curses.curs_set"):
            result = _run_review(
                screen,
                groups,
                title="Review",
                allow_execution=allow_execution,
            )
        return result, screen

    def test_drill_down_toggle_and_execution_confirmation(self) -> None:
        safe = _item("safe", "safe", preselected=True)
        confirm = _item("confirm", "confirm", preselected=False)
        groups = (ReviewGroup("dev", "Dev", (safe, confirm)),)

        result, screen = self._run(
            groups,
            [
                curses.KEY_RIGHT,
                curses.KEY_DOWN,
                ord(" "),
                curses.KEY_LEFT,
                10,
                ord("y"),
            ],
            allow_execution=True,
        )

        self.assertFalse(result.cancelled)
        self.assertTrue(result.submitted)
        self.assertTrue(result.execution_confirmed)
        self.assertEqual(result.selected, (safe, confirm))
        self.assertTrue(any("Dev" in line for line in screen.lines))

    def test_without_yes_confirmation_only_submits_preview(self) -> None:
        safe = _item("safe", "safe", preselected=True)
        confirm = _item("confirm", "confirm", preselected=False)
        groups = (ReviewGroup("dev", "Dev", (safe, confirm)),)

        result, _ = self._run(
            groups,
            [ord(" "), 10, 10],
            allow_execution=False,
        )

        self.assertTrue(result.submitted)
        self.assertFalse(result.execution_confirmed)
        self.assertEqual(result.selected, (safe, confirm))

    def test_critical_requires_bang_after_yes_confirmation(self) -> None:
        critical = _item("critical", "critical", preselected=True)
        groups = (ReviewGroup("critical", "Critical", (critical,)),)

        result, screen = self._run(
            groups,
            [10, ord("y"), ord("!")],
            allow_execution=True,
        )

        self.assertTrue(result.execution_confirmed)
        self.assertEqual(result.selected, (critical,))
        self.assertTrue(any("二次确认" in line for line in screen.lines))

    def test_non_actionable_items_cannot_be_selected(self) -> None:
        blocked = _item(
            "volume", "critical", preselected=False, actionable=False
        )
        groups = (ReviewGroup("docker", "Docker", (blocked,)),)

        result, screen = self._run(
            groups,
            [ord(" "), curses.KEY_RIGHT, ord(" "), curses.KEY_LEFT, 10, ord("y")],
            allow_execution=True,
        )

        self.assertEqual(result.selected, ())
        self.assertTrue(result.execution_confirmed)
        self.assertTrue(any("不可执行" in line for line in screen.lines))

    def test_bulk_actions_skip_exact_selection_items(self) -> None:
        safe = _item("safe", "safe", preselected=False)
        environment = _item(
            "environment",
            "confirm",
            preselected=False,
            requires_explicit_selection=True,
        )
        groups = (ReviewGroup("dev", "Dev", (safe, environment)),)

        for bulk_key in (ord(" "), ord("a")):
            with self.subTest(bulk_key=bulk_key):
                result, screen = self._run(
                    groups,
                    [bulk_key, 10, ord("y")],
                    allow_execution=True,
                )

                self.assertEqual(result.selected, (safe,))
                self.assertTrue(result.execution_confirmed)
                self.assertTrue(
                    any("批量选择" in line for line in screen.lines)
                )

    def test_exact_selection_item_can_be_toggled_individually(self) -> None:
        environment = _item(
            "environment",
            "confirm",
            preselected=False,
            requires_explicit_selection=True,
        )
        groups = (ReviewGroup("dev", "Dev", (environment,)),)

        result, screen = self._run(
            groups,
            [curses.KEY_RIGHT, ord(" "), curses.KEY_LEFT, 10, ord("y")],
            allow_execution=True,
        )

        self.assertEqual(result.selected, (environment,))
        self.assertTrue(result.execution_confirmed)
        self.assertTrue(any("需逐项选择" in line for line in screen.lines))

    def test_q_cancels_without_selection(self) -> None:
        safe = _item("safe", "safe", preselected=True)
        groups = (ReviewGroup("dev", "Dev", (safe,)),)

        result, _ = self._run(groups, [ord("q")], allow_execution=True)

        self.assertTrue(result.cancelled)
        self.assertFalse(result.submitted)
        self.assertEqual(result.selected, ())

    def test_wrapper_reports_terminal_initialization_failure(self) -> None:
        safe = _item("safe", "safe", preselected=True)
        groups = (ReviewGroup("dev", "Dev", (safe,)),)

        with mock.patch(
            "curses.wrapper", side_effect=curses.error("no tty")
        ), self.assertRaisesRegex(TUIUnavailable, "无法启动"):
            review_cleanup(
                groups,
                title="Review",
                allow_execution=False,
            )


if __name__ == "__main__":
    unittest.main()
