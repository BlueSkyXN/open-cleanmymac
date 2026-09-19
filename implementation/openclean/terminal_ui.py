"""CLI/TUI 共用的终端列宽和绘制样式；不参与选择或执行判定。"""
from __future__ import annotations

import curses
import os
import unicodedata

MIN_WIDTH = 48
MIN_HEIGHT = 14
_colors_enabled = False
_theme = "auto"
ROLE_PAIRS = {"title": 1, "warning": 3, "danger": 4, "selected": 5}
# 使用 256 色索引，避开可自定义的前 16 个 ANSI 色及粗体映射为亮色的设置。
# auto 始终沿用终端默认前景/背景，不从 TERM、COLORFGBG 或 macOS 外观猜测背景。
COLOR_SCHEMES = {
    "light": {"title": 24, "warning": 94, "danger": 124, "selected": 22},
    "dark": {"title": 81, "warning": 221, "danger": 210, "selected": 114},
}


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
    global _colors_enabled, _theme
    _colors_enabled = False
    requested = os.environ.get("OPENCLEAN_THEME", "auto").strip().lower()
    _theme = requested if requested in {"auto", *COLOR_SCHEMES} else "auto"
    try:
        if not curses.has_colors():
            return
        curses.start_color()
        # curses.wrapper 会先初始化白字黑底的 pair 0；NO_COLOR 也必须恢复终端默认色。
        curses.use_default_colors()
        if "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb" or \
                _theme == "auto" or getattr(curses, "COLORS", 0) < 256:
            return
        for role, foreground in COLOR_SCHEMES[_theme].items():
            curses.init_pair(ROLE_PAIRS[role], foreground, -1)
        _colors_enabled = True
    except curses.error:
        pass


def style(role: str) -> int:
    # 默认色反白可随 iTerm2/Terminal 的前景与背景变化，无需读取 stdin 的终端查询响应。
    if role == "focus":
        return curses.A_REVERSE
    if role == "muted":
        return curses.A_NORMAL
    # 不叠加 A_BOLD/A_DIM：iTerm2 可独立配置粗体颜色和 faint 强度。
    attribute = curses.A_UNDERLINE if role in {"title", "danger"} else curses.A_NORMAL
    if _colors_enabled and role in ROLE_PAIRS:
        return attribute | curses.color_pair(ROLE_PAIRS[role])
    return attribute


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
