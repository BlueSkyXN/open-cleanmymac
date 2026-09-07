from __future__ import annotations

import curses
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from openclean.models import FILESYSTEM_SUBSET_DIAGNOSTIC_KINDS, Item
from openclean.tui import (
    ReviewGroup,
    TUIUnavailable,
    MENU_ITEMS,
    MenuChoice,
    _draw_item_details,
    _run_menu,
    _run_review,
    _wrap_detail_line,
    choose_menu,
    item_detail_lines,
    item_diagnostic_summary,
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

    def test_details_scroll_and_return_preserve_cursor_and_selection(self) -> None:
        safe = _item("safe", "safe", preselected=True)
        blocked = replace(_item("blocked", "critical", preselected=False, actionable=False),
                          note="这是一条已有证据", age_days=14, open_handle_count=0)
        groups = (ReviewGroup("dev", "Dev", (safe, blocked)),)
        result, screen = self._run(
            groups,
            [curses.KEY_RIGHT, curses.KEY_DOWN, ord("i"), curses.KEY_DOWN,
             ord(" "), 10, ord("a"), curses.KEY_UP, curses.KEY_RESIZE,
             27, ord(" "), curses.KEY_LEFT, 10, 10],
            allow_execution=False,
        )
        self.assertEqual(result.selected, (safe,))
        self.assertFalse(result.execution_confirmed)
        self.assertTrue(any("这是一条已有证据" in line for line in screen.lines))
        self.assertTrue(any("打开句柄数：0" in line for line in screen.lines))

    def test_details_quit_cancels_and_empty_review_does_not_open_screen(self) -> None:
        groups = (ReviewGroup("dev", "Dev", (_item("safe", "safe", preselected=True),)),)
        result, _ = self._run(groups, [curses.KEY_RIGHT, ord("I"), ord("q")],
                              allow_execution=True)
        self.assertTrue(result.cancelled)
        self.assertEqual(result.selected, ())
        with mock.patch("curses.wrapper") as wrapper:
            empty = review_cleanup((), title="Empty", allow_execution=False)
        wrapper.assert_not_called()
        self.assertEqual(empty.selected, ())
        self.assertFalse(empty.execution_confirmed)

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


class MenuTests(unittest.TestCase):
    def _run(self, keys, *, menu="root", cursor=0, screen=None):
        screen = screen or _FakeScreen(keys)
        with mock.patch("curses.curs_set"):
            return _run_menu(screen, menu=menu, cursor=cursor)

    def test_five_entries_navigation_and_enter(self) -> None:
        for index, (action, _) in enumerate(MENU_ITEMS["root"]):
            with self.subTest(action=action):
                self.assertEqual(self._run([curses.KEY_DOWN] * index + [10]),
                                 MenuChoice(action, index))
        self.assertEqual(self._run([curses.KEY_UP, 10]), MenuChoice("clean", 0))
        self.assertEqual(self._run([ord("x"), curses.KEY_DOWN, 10], cursor=999),
                         MenuChoice("config", 4))

    def test_more_optimize_back_and_quit(self) -> None:
        self.assertEqual(self._run([ord("m")]).action, "more")
        self.assertEqual(self._run([10], menu="more").action, "cat")
        for menu in ("root", "more", "optimize"):
            for key in (27, ord("q"), ord("Q")):
                with self.subTest(menu=menu, key=key):
                    self.assertEqual(self._run([key], menu=menu).action,
                                     "quit" if menu == "root" else "back")
        self.assertEqual(self._run([10], menu="optimize").action, "ram")
        self.assertEqual(self._run([curses.KEY_DOWN, 10], menu="optimize").action, "purgeable")

    def test_small_screen_and_resize_keep_selection(self) -> None:
        screen = _FakeScreen([curses.KEY_DOWN, curses.KEY_RESIZE, 10], height=1, width=1)
        with mock.patch.object(screen, "getmaxyx", side_effect=[(1, 1), (4, 10), (24, 120)]):
            self.assertEqual(self._run([], screen=screen), MenuChoice("purge", 1))

    def test_wrapper_returns_choice_or_reports_initialization_failure(self) -> None:
        choice = MenuChoice("clean", 0)
        with mock.patch("curses.wrapper", return_value=choice) as wrapper:
            self.assertEqual(choose_menu("root"), choice)
        wrapper.assert_called_once()
        for error in (curses.error("no tty"), OSError("no terminal")):
            with mock.patch("curses.wrapper", side_effect=error), self.assertRaises(TUIUnavailable):
                choose_menu("root")


class ItemDetailTests(unittest.TestCase):
    def test_long_unicode_path_wraps_without_loss_and_resizes(self) -> None:
        path = "/Preview/" + "长路径-with-spaces/" * 20 + "last-entry"
        item = replace(_item("safe", "safe", preselected=False), path=Path(path))
        lines = item_detail_lines(item)
        self.assertIn(path, "\n".join(lines))
        self.assertIn("年龄（天）：未知", "\n".join(lines))
        text = "路径：" + path
        self.assertEqual("".join(_wrap_detail_line(text, 12)), text)
        self.assertEqual("".join(_wrap_detail_line("a\x1b\nb", 12)), r"a\x1b\nb")
        screen = _FakeScreen([], height=6, width=20)
        with mock.patch.object(screen, "getmaxyx", side_effect=[(6, 20), (24, 120), (1, 1)]):
            for offset in (9999, 9999, 0):
                actual, maximum = _draw_item_details(screen, item, offset)
                self.assertLessEqual(actual, maximum)
        self.assertTrue(any("last-entry" in line for line in screen.lines))

    def test_all_diagnostic_kinds_use_existing_evidence_without_mutating_item(self) -> None:
        cases = [
            ("retention", dict(retention_file_count=4, retention_7d_bytes=4096,
                               retention_14d_bytes=2048, retention_30d_bytes=0), "重叠桶"),
            ("sqlite_freelist", dict(sqlite_page_size=4096, sqlite_page_count=10,
                                     sqlite_freelist_count=0, sqlite_internal_free_bytes=0,
                                     sqlite_internal_free_ratio=0.0, sqlite_wal_bytes=0), "0.0%"),
            ("codex_transient", dict(total_count=3, measured_count=2, measurement_complete=False), "不完整"),
            ("crashpad_pairing", dict(total_count=4, paired_artifact_count=2, recent_artifact_count=0), "4 / 2 / 0"),
            ("open_unlinked", dict(logical_size=4096, total_count=2, related_process_count=1), "非可回收量"),
            ("updater_temp", dict(updater_status="version_unknown"), "已安装版本：未知"),
        ]
        for kind, metadata, expected in cases:
            with self.subTest(kind=kind):
                subset = kind in FILESYSTEM_SUBSET_DIAGNOSTIC_KINDS
                item = Item(path=Path("/Preview/diagnostic"), size=0, category=kind,
                            actionable=False, diagnostic_kind=kind,
                            resource_kind="filesystem_subset" if subset else "filesystem",
                            **metadata)
                before = item.__dict__.copy()
                details = "\n".join(item_detail_lines(item))
                self.assertIn(expected, details)
                self.assertIn("只读诊断", details)
                self.assertEqual(item.__dict__, before)
                self.assertTrue(item_diagnostic_summary(item))
                if subset:
                    self.assertIn("不代表整个父目录", details)

    def test_identifier_and_blocked_resource_are_not_mislabelled_as_cleanup(self) -> None:
        item = Item(path=None, identifier="docker:volumes", resource_kind="docker",
                    size=1024, category="Volumes", actionable=False,
                    action_block_reason="Volumes 永远拒绝")
        details = "\n".join(item_detail_lines(item))
        self.assertIn("docker:volumes", details)
        self.assertIn("被保护或阻断", details)
        self.assertIn("Volumes 永远拒绝", details)


if __name__ == "__main__":
    unittest.main()
