"""CLI/TUI 共用的终端列宽和绘制样式；不参与选择或执行判定。"""
from __future__ import annotations

import curses
import os
import unicodedata

MIN_WIDTH = 48
MIN_HEIGHT = 14
_colors_enabled = False
ROLE_PAIRS = {"title": 1, "focus": 2, "warning": 3, "danger": 4, "selected": 5}


def safe_text(value: object) -> str:
    return "".join(char if char.isprintable() else repr(char)[1:-1] for char in str(value))


def cell_width(value: str) -> int:
    return sum(0 if unicodedata.combining(char) else
               2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in value)


def clip_cells(value: object, width: int, *, tail: bool = False) -> str:
    text = safe_text(value)
    if width <= 0:
        return ""
    if cell_width(text) <= width:
        return text
    result = ""
    for char in reversed(text) if tail else text:
        if cell_width(result) + cell_width(char) > width - 1:
            break
        result = char + result if tail else result + char
    return "…" + result if tail else result + "…"


def pad_cells(value: object, width: int, *, right: bool = False) -> str:
    text = safe_text(value)
    padding = " " * max(0, width - cell_width(text))
    return padding + text if right else text + padding


def wrap_cells(value: object, width: int) -> list[str]:
    text = safe_text(value)
    lines: list[str] = []
    current = ""
    columns = 0
    for char in text:
        size = cell_width(char)
        if current and columns + size > max(1, width):
            lines.append(current)
            current, columns = "", 0
        current += char
        columns += size
    lines.append(current)
    return lines


def init_styles() -> None:
    global _colors_enabled
    _colors_enabled = False
    if "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb":
        return
    try:
        if not curses.has_colors():
            return
        curses.start_color()
        curses.use_default_colors()
        for role, foreground, background in (
            ("title", curses.COLOR_CYAN, -1),
            ("focus", curses.COLOR_BLACK, curses.COLOR_CYAN),
            ("warning", curses.COLOR_YELLOW, -1),
            ("danger", curses.COLOR_RED, -1),
            ("selected", curses.COLOR_GREEN, -1),
        ):
            curses.init_pair(ROLE_PAIRS[role], foreground, background)
        _colors_enabled = True
    except curses.error:
        pass


def style(role: str) -> int:
    if role == "muted":
        return curses.A_DIM
    attribute = curses.A_BOLD if role in {"title", "focus", "warning", "danger", "selected"} else 0
    if _colors_enabled and role in ROLE_PAIRS:
        return attribute | curses.color_pair(ROLE_PAIRS[role])
    return attribute | (curses.A_REVERSE if role == "focus" else 0)


def draw_text(screen, row: int, column: int, value: object, width: int, role: str = "plain") -> None:
    height, screen_width = screen.getmaxyx()
    if not 0 <= row < height or column < 0:
        return
    available = min(width, screen_width) - column - 1
    text = clip_cells(value, available)
    if role == "focus":
        text = pad_cells(text, available)
    if not text:
        return
    try:
        # curses 的 n 限制可能按 UTF-8 字节计；先按显示列截断，再交付完整字节序列。
        screen.addnstr(row, column, text, len(text.encode("utf-8")), style(role))
    except curses.error:
        pass


def draw_footer(screen, hints: str) -> int:
    height, width = screen.getmaxyx()
    limit = max(1, width - 2)
    lines: list[str] = []
    current = ""
    for hint in hints.split(" · "):
        proposed = f"{current} · {hint}" if current else hint
        if current and cell_width(proposed) > limit:
            lines.append(current)
            current = ""
        parts = wrap_cells(hint, limit)
        if len(parts) > 1:
            lines.extend(parts[:-1])
            current = parts[-1]
        else:
            current = f"{current} · {hint}" if current else hint
    if current:
        lines.append(current)
    start = max(0, height - len(lines))
    for row, line in enumerate(lines, start):
        draw_text(screen, row, 1, line, width, "muted")
    return start


def small_screen(screen) -> bool:
    height, width = screen.getmaxyx()
    return width < MIN_WIDTH or height < MIN_HEIGHT


def draw_small_screen(screen) -> None:
    screen.erase()
    _, width = screen.getmaxyx()
    draw_text(screen, 0, 0, "请放大终端窗口", width, "warning")
    draw_text(screen, 2, 0, f"至少 {MIN_WIDTH} 列 × {MIN_HEIGHT} 行", width)
    draw_text(screen, 4, 0, "选择已保留 · Q 退出", width)
    screen.refresh()
