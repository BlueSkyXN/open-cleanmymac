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

from openclean import terminal_ui
from openclean.analyzer import SpaceAnalysis, SpaceEntry
from openclean.cli import _print_clean_report, _print_report
from openclean.models import Item, ScanIssue, ScanResult
from openclean.space_tui import _draw_browser
from openclean.terminal_ui import cell_width, clip_cells, draw_footer, init_styles, pad_cells, safe_text, style
from openclean.tui import ReviewGroup, _draw_confirmation, _draw_items, _item_key, _run_review
from scripts.capture_tui_assets import GridScreen, cell_colors, indexed_color, preview_styles


class TerminalThemeTests(unittest.TestCase):
    def setUp(self) -> None:
        for patcher in (
            mock.patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True),
            mock.patch.object(terminal_ui, "_colors_enabled", False),
            mock.patch.object(terminal_ui, "_theme", "auto"),
            mock.patch("curses.has_colors", return_value=True),
            mock.patch("curses.start_color"),
            mock.patch("curses.use_default_colors"),
            mock.patch("curses.COLORS", 256, create=True),
            mock.patch("curses.color_pair", side_effect=lambda pair: pair << 8),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_auto_uses_native_colors_without_guessing_terminal_background(self) -> None:
        for theme in ("auto", "unknown"):
            for hint in ("0;15", "15;0"):
                with self.subTest(theme=theme, hint=hint), mock.patch.dict(os.environ, {
                    "OPENCLEAN_THEME": theme, "COLORFGBG": hint, "TERM_PROGRAM": "iTerm.app",
                }), mock.patch("curses.init_pair") as register, mock.patch("curses.init_color") as palette:
                    init_styles()
                    register.assert_not_called()
                    palette.assert_not_called()
                    self.assertFalse(terminal_ui._colors_enabled)
                    self.assertEqual(style("focus"), curses.A_REVERSE)

    def test_explicit_schemes_keep_default_background_and_native_focus(self) -> None:
        for theme in ("light", "dark"):
            with self.subTest(theme=theme), preview_styles(theme) as pairs:
                self.assertTrue(terminal_ui._colors_enabled)
                for role, foreground in terminal_ui.COLOR_SCHEMES[theme].items():
                    self.assertGreaterEqual(foreground, 16)
                    self.assertEqual(pairs[terminal_ui.ROLE_PAIRS[role]], (foreground, -1))
                    self.assertTrue(style(role) & curses.A_COLOR)
                self.assertEqual(style("focus"), curses.A_REVERSE)
                for role in ("plain", "title", "warning", "danger", "selected", "muted", "focus"):
                    self.assertFalse(style(role) & (curses.A_BOLD | curses.A_DIM), role)

    def test_color_overrides_and_limited_terminals_fall_back(self) -> None:
        for environment, colors, supported in (
            ({"NO_COLOR": ""}, 256, True),
            ({"TERM": "dumb"}, 256, True),
            ({}, 8, True),
            ({}, 16, True),
            ({}, 256, False),
        ):
            with self.subTest(environment=environment, colors=colors, supported=supported), \
                    mock.patch.dict(os.environ, {"OPENCLEAN_THEME": "light", **environment}), \
                    mock.patch("curses.COLORS", colors), \
                    mock.patch("curses.has_colors", return_value=supported), \
                    mock.patch("curses.init_pair") as register:
                init_styles()
                register.assert_not_called()
                self.assertFalse(terminal_ui._colors_enabled)
                self.assertEqual(style("focus"), curses.A_REVERSE)
                self.assertEqual(style("muted"), curses.A_NORMAL)

    def test_initialization_failure_resets_previously_enabled_colors(self) -> None:
        for operation in ("start_color", "use_default_colors", "init_pair"):
            with self.subTest(operation=operation), \
                    mock.patch.dict(os.environ, {"OPENCLEAN_THEME": "dark"}), \
                    mock.patch.object(terminal_ui, "_colors_enabled", True), \
                    mock.patch(f"curses.{operation}", side_effect=curses.error):
                init_styles()
                self.assertFalse(terminal_ui._colors_enabled)
                self.assertFalse(style("title") & curses.A_COLOR)
                self.assertEqual(style("focus"), curses.A_REVERSE)

    def test_reference_light_and_dark_text_meets_contrast_target(self) -> None:
        def luminance(color):
            components = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
            linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
                      for value in components]
            return sum(value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722)))

        for appearance in ("light", "dark"):
            for theme in ("auto", appearance):
                with preview_styles(theme) as pairs:
                    screen = GridScreen(appearance=appearance, color_pairs=pairs)
                    for role in ("plain", "title", "warning", "danger", "selected", "muted", "focus"):
                        with self.subTest(appearance=appearance, theme=theme, role=role):
                            values = sorted(luminance(color) for color in cell_colors(screen, style(role)))
                            self.assertGreaterEqual((values[1] + 0.05) / (values[0] + 0.05), 4.5)

    def test_preview_colors_follow_production_pairs_and_reverse_video(self) -> None:
        for appearance in ("light", "dark"):
            with preview_styles(appearance) as pairs:
                screen = GridScreen(appearance=appearance, color_pairs=pairs)
                self.assertEqual(cell_colors(screen, style("focus")),
                                 (screen.profile["background"], screen.profile["foreground"]))
                self.assertEqual(cell_colors(screen, style("selected")),
                                 (indexed_color(terminal_ui.COLOR_SCHEMES[appearance]["selected"]),
                                  screen.profile["background"]))


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

    def test_no_color_restores_default_background_without_custom_colors(self) -> None:
        with mock.patch.dict(os.environ, {"NO_COLOR": ""}), \
                mock.patch("curses.has_colors", return_value=True), mock.patch("curses.start_color"), \
                mock.patch("curses.use_default_colors") as defaults, mock.patch("curses.init_pair") as register:
            init_styles()
            self.assertTrue(style("focus") & curses.A_REVERSE)
            defaults.assert_called_once_with()
            register.assert_not_called()

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

    def test_analyze_readonly_marker_keeps_capacity_columns_aligned(self) -> None:
        first = Item(Path("/tmp/preview/open"), 4096, "空间")
        second = Item(Path("/tmp/preview/locked"), 4096, "空间", actionable=False,
                      action_block_reason="只读")
        analysis = SpaceAnalysis(Path("/tmp/preview"), entries=[SpaceEntry(first, 50), SpaceEntry(second, 50)])
        screen = GridScreen()
        with mock.patch("openclean.space_tui._is_directory", return_value=False):
            _draw_browser(screen, analysis, analysis.entries, 0, {}, "")
        rows = [line for line in screen.plain_text().splitlines() if "4.0KB" in line]
        self.assertEqual(len(rows), 2)
        self.assertEqual(cell_width(rows[0].split("4.0KB")[0]), cell_width(rows[1].split("4.0KB")[0]))

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
        for environment, colored in (
            ({"OPENCLEAN_THEME": "auto"}, False),
            ({"OPENCLEAN_THEME": "light"}, True),
            ({"OPENCLEAN_THEME": "dark"}, True),
            ({"OPENCLEAN_THEME": "light", "NO_COLOR": ""}, False),
        ):
            with self.subTest(environment=environment):
                self.run_native_session(environment, colored)

    def run_native_session(self, environment: dict[str, str], colored: bool) -> None:
        program = '''
import curses, json
from pathlib import Path
from openclean import terminal_ui
from openclean.models import Item
from openclean.tui import ReviewGroup, _draw_items, _run_review
from openclean.terminal_ui import init_styles
item = Item(Path('/tmp/preview/cache'), 4096, '中文缓存', preselected=False)
groups = (ReviewGroup('dev', '开发工具', (item,)),)
def session(screen):
    init_styles()
    _draw_items(screen, groups, 0, 0, set(), 'Clean')
    # 行 3 是“本分类已选”范围汇总，条目行从行 5 开始。
    text = screen.instr(5, 0).decode('utf-8')
    result = _run_review(screen, groups, title='Clean', allow_execution=False)
    return {'chinese': '中文缓存' in text, 'selected': len(result.selected),
            'submitted': result.submitted, 'executed': result.execution_confirmed,
            'colored': terminal_ui._colors_enabled, 'default_pair': curses.pair_content(0)}
print('FRAME_RESULT:' + json.dumps(curses.wrapper(session)))
'''
        with tempfile.TemporaryDirectory() as temporary:
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
            env = {**os.environ, "HOME": temporary, "TERM": "xterm-256color"}
            env.pop("NO_COLOR", None)
            env.update(environment)
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
            self.assertEqual(payload, {"chinese": True, "selected": 1, "submitted": True,
                                       "executed": False, "colored": colored, "default_pair": [-1, -1]})
            self.assertNotIn(b"]11;?", captured)  # 不查询背景，避免响应与键盘输入混用。


if __name__ == "__main__":
    unittest.main()
