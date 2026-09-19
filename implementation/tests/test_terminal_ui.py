from __future__ import annotations

import contextlib
import curses
import fcntl
import io
import json
import os
import pty
import select
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from pathlib import Path
from unittest import mock

from openclean.analyzer import SpaceAnalysis, SpaceEntry
from openclean.cli import _print_clean_report, _print_report
from openclean.models import Item, ScanIssue, ScanResult
from openclean.space_tui import _draw_browser
from openclean.terminal_ui import cell_width, clip_cells, draw_footer, init_styles, pad_cells, safe_text, style
from openclean.tui import ReviewGroup, _draw_confirmation, _draw_items, _item_key, _run_review
from scripts.capture_tui_assets import GridScreen


class TerminalPresentationTests(unittest.TestCase):
    def items(self):
        return (
            Item(Path("/tmp/preview/cache"), 4096, "中文缓存", preselected=True, domain="developer"),
            Item(Path("/tmp/preview/blocked"), 8192, "Protected cache", actionable=False,
                 action_block_reason="正在运行，不能处理", domain="developer"),
        )

    def test_column_width_handles_cjk_combining_and_control_characters(self) -> None:
        self.assertEqual(cell_width("中文e\u0301"), 5)
        self.assertEqual(cell_width(pad_cells("中文", 10)), 10)
        self.assertEqual(cell_width(pad_cells("中文", 10, right=True)), 10)
        for width in range(1, 15):
            for tail in (False, True):
                self.assertLessEqual(cell_width(clip_cells("中文/long-name", width, tail=tail)), width)
        self.assertEqual(safe_text("a\x1b[31m\nb"), r"a\x1b[31m\nb")

    def test_footer_retains_quit_and_back_actions_on_narrow_screens(self) -> None:
        for width in (48, 80, 120):
            with self.subTest(width=width):
                screen = GridScreen(height=18, width=width)
                start = draw_footer(screen, "↑↓ 移动 · Space/Enter 切换 · I 只读详情 · A 批量选择 · ←/Esc 返回 · Q 退出")
                text = screen.plain_text().replace("\n", "")
                self.assertIn("Q 退出", text)
                self.assertIn("Esc 返回", text)
                self.assertGreaterEqual(start, 14)

    def test_items_align_sizes_and_focus_has_non_color_cue(self) -> None:
        items = self.items()
        groups = (ReviewGroup("developer", "开发工具", items),)
        for width in (48, 80, 120):
            with self.subTest(width=width), mock.patch("openclean.terminal_ui._colors_enabled", False):
                screen = GridScreen(height=18, width=width)
                _draw_items(screen, groups, 0, 1, {_item_key(items[0])}, "Clean")
                rows = screen.plain_text().splitlines()
                first = next(row for row in rows if "中文缓存" in row)
                second = next(row for row in rows if "Protected" in row)
                self.assertEqual(cell_width(first.split("4.0KB")[0]), cell_width(second.split("8.0KB")[0]))
                self.assertIn("不可执行", screen.plain_text())
                self.assertIn("正在运行", screen.plain_text())
                self.assertTrue(any(attribute & curses.A_REVERSE for row in screen.attributes for attribute in row))

    def test_confirmation_warning_survives_long_selection_and_narrow_width(self) -> None:
        items = tuple(Item(Path(f"/tmp/preview/{index}"), 100, "item") for index in range(30))
        groups = (ReviewGroup("developer", "开发", items),)
        for width in (48, 80, 120):
            screen = GridScreen(height=18, width=width)
            _draw_confirmation(screen, groups, {_item_key(item) for item in items}, "Clean", True)
            text = screen.plain_text().replace("\n", "")
            self.assertIn("永久删除", text)
            self.assertIn("按 Y", text)
            self.assertIn("返回列表", text)

    def test_no_color_skips_color_initialization_but_keeps_focus(self) -> None:
        with mock.patch.dict(os.environ, {"NO_COLOR": ""}), mock.patch("curses.start_color") as start:
            init_styles()
            self.assertTrue(style("focus") & curses.A_REVERSE)
            start.assert_not_called()

    def test_small_review_ignores_hidden_confirmation_until_resize(self) -> None:
        item = self.items()[0]
        screen = GridScreen(height=8, width=30)
        keys = iter((10, ord("y"), ord("!"), curses.KEY_RESIZE, 10, 10))

        def getch():
            key = next(keys)
            if key == curses.KEY_RESIZE:
                screen.height, screen.width = 24, 80
            return key

        with mock.patch.object(screen, "getch", side_effect=getch), mock.patch("curses.curs_set"):
            result = _run_review(screen, (ReviewGroup("developer", "Dev", (item,)),),
                                 title="Clean", allow_execution=False)
        self.assertTrue(result.submitted)
        self.assertFalse(result.execution_confirmed)
        self.assertEqual(result.selected, (item,))

    def test_analyze_shows_incomplete_warning_and_compact_names(self) -> None:
        item = self.items()[0]
        analysis = SpaceAnalysis(Path("/tmp/preview"), entries=[SpaceEntry(item, 100)], issues=[
            ScanIssue("permission_denied", "部分目录无法访问", path=Path("/tmp/preview/private")),
        ])
        screen = GridScreen()
        with mock.patch("openclean.space_tui._is_directory", return_value=False):
            _draw_browser(screen, analysis, analysis.entries, 0, {}, "")
        text = screen.plain_text()
        self.assertIn("分析不完整", text)
        self.assertIn("部分目录无法访问", text)
        self.assertIn("cache", text)
        self.assertIn("Delete 汇总", text)

    def test_text_reports_keep_full_paths_and_separate_non_actionable_totals(self) -> None:
        result = ScanResult(items=list(self.items()))
        for printer in (lambda: _print_report(result, False, ["developer"]),
                        lambda: _print_clean_report(result, False, "dev")):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                printer()
            text = output.getvalue()
            self.assertIn("当前可执行 4.0KB", text)
            self.assertIn("只读/阻断 8.0KB", text)
            self.assertIn("[!]", text)
            self.assertIn(str(result.items[0].path), text)
            self.assertNotIn("\x1b", text)
        before = tuple(result.items)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            _print_report(result, True, ["developer"])
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["reclaimable_bytes"], 4096)
        self.assertEqual(payload["items"][0]["actionable"], False)
        self.assertEqual(tuple(result.items), before)

    @unittest.skipUnless(sys.platform == "darwin", "原生 macOS curses PTY 验证")
    def test_native_curses_keeps_chinese_and_preview_keyboard_flow(self) -> None:
        program = '''
import curses, json
from pathlib import Path
from openclean.models import Item
from openclean.tui import ReviewGroup, _draw_items, _run_review
from openclean.terminal_ui import init_styles
item = Item(Path('/tmp/preview/cache'), 4096, '中文缓存', preselected=False)
groups = (ReviewGroup('dev', '开发工具', (item,)),)
def session(screen):
    init_styles()
    _draw_items(screen, groups, 0, 0, set(), 'Clean')
    text = screen.instr(4, 0).decode('utf-8')
    result = _run_review(screen, groups, title='Clean', allow_execution=False)
    return {'chinese': '中文缓存' in text, 'selected': len(result.selected),
            'submitted': result.submitted, 'executed': result.execution_confirmed}
print('FRAME_RESULT:' + json.dumps(curses.wrapper(session)))
'''
        with tempfile.TemporaryDirectory() as temporary:
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
            env = {**os.environ, "HOME": temporary, "TERM": "xterm-256color"}
            process = subprocess.Popen([sys.executable, "-c", program], stdin=slave, stdout=slave,
                                       stderr=slave, env=env)
            os.close(slave)
            captured = b""
            sent = False
            deadline = time.monotonic() + 10
            try:
                while time.monotonic() < deadline:
                    if select.select([master], [], [], 0.1)[0]:
                        try:
                            chunk = os.read(master, 65536)
                        except OSError:
                            break
                        if not chunk:
                            break
                        captured += chunk
                        if not sent and b"Clean" in captured:
                            os.write(master, b"l h\r\r")
                            sent = True
                    elif process.poll() is not None:
                        break
                self.assertEqual(process.wait(timeout=2), 0, captured.decode("utf-8", errors="replace"))
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                os.close(master)
            payload = json.loads(captured.split(b"FRAME_RESULT:")[-1].strip())
            self.assertEqual(payload, {"chinese": True, "selected": 1, "submitted": True, "executed": False})


if __name__ == "__main__":
    unittest.main()
