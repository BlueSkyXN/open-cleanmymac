"""用固定合成数据生成可审计的 TUI 文档 SVG。

资产直接调用生产 TUI 的 ``_draw_*`` 函数，不启动真实终端，不读取用户目录，
也不连接 Docker、File Provider 或网络服务。
"""
from __future__ import annotations

import argparse
import contextlib
import curses
import html
import io
import sys
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

IMPLEMENTATION_ROOT = Path(__file__).resolve().parents[1]
if str(IMPLEMENTATION_ROOT) not in sys.path:
    sys.path.insert(0, str(IMPLEMENTATION_ROOT))

REPOSITORY_ROOT = IMPLEMENTATION_ROOT.parent
DEFAULT_OUTPUT_DIR = REPOSITORY_ROOT / "docs" / "assets"
ASSET_NAMES = (
    "tui-menu.svg",
    "tui-clean-review.svg",
    "tui-clean-confirm.svg",
    "tui-analyze.svg",
    "tui-analyze-loading.svg",
    "cli-scan.svg",
    "tui-clean-review-light.svg",
    "cli-scan-light.svg",
    "tui-clean-review-colors-light.svg",
    "tui-clean-review-colors-dark.svg",
)
REFERENCE_PROFILES = {
    "dark": {"background": "#0d1117", "foreground": "#c9d1d9", "chrome": "#161b22", "caption": "#8b949e"},
    "light": {"background": "#ffffff", "foreground": "#24292f", "chrome": "#f0f3f6", "caption": "#57606a"},
}
TERMINAL_HEIGHT = 24
TERMINAL_WIDTH = 120
CELL_WIDTH = 10
CELL_HEIGHT = 22
LEFT_MARGIN = 24
TOP_MARGIN = 58


def _cell_width(character: str) -> int:
    if unicodedata.combining(character):
        return 0
    return 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1


class GridScreen:
    """实现 TUI 绘制函数所需的最小、位置感知 screen 协议。"""

    def __init__(
        self,
        height: int = TERMINAL_HEIGHT,
        width: int = TERMINAL_WIDTH,
        appearance: str = "dark",
        color_pairs: dict[int, tuple[int, int]] | None = None,
    ) -> None:
        self.height = height
        self.width = width
        self.profile = REFERENCE_PROFILES[appearance]
        self.color_pairs = color_pairs or {}
        self.keypad_enabled = False
        self.erase()

    def keypad(self, enabled: bool) -> None:
        self.keypad_enabled = enabled

    def getmaxyx(self) -> tuple[int, int]:
        return self.height, self.width

    def erase(self) -> None:
        self.cells: list[list[str | None]] = [
            [None for _ in range(self.width)]
            for _ in range(self.height)
        ]
        self.attributes = [[0 for _ in range(self.width)] for _ in range(self.height)]

    def addnstr(
        self,
        row: int,
        column: int,
        text: str,
        length: int,
        attribute: int = 0,
    ) -> None:
        if not 0 <= row < self.height or column < 0 or column >= self.width:
            return
        limit = min(self.width - 1, column + max(0, length))
        cursor = column
        previous: int | None = None
        for character in text:
            width = _cell_width(character)
            if width == 0:
                if previous is not None and self.cells[row][previous]:
                    self.cells[row][previous] += character
                continue
            if cursor + width > limit:
                break
            self.cells[row][cursor] = character
            self.attributes[row][cursor] = attribute
            previous = cursor
            for continuation in range(1, width):
                self.cells[row][cursor + continuation] = ""
                self.attributes[row][cursor + continuation] = attribute
            cursor += width

    def refresh(self) -> None:
        pass

    def getch(self) -> int:
        raise RuntimeError("文档资产捕获不会读取键盘")

    def plain_text(self) -> str:
        lines = []
        for row in self.cells:
            line = "".join(cell if cell is not None else " " for cell in row)
            lines.append(line.rstrip())
        return "\n".join(lines).rstrip()


def indexed_color(index: int) -> str:
    """标准 xterm 256 色立方体/灰阶；预览不猜测用户可配置的前 16 色。"""
    if 16 <= index <= 231:
        value = index - 16
        levels = (0, 95, 135, 175, 215, 255)
        rgb = (levels[value // 36], levels[(value // 6) % 6], levels[value % 6])
    elif 232 <= index <= 255:
        rgb = (8 + 10 * (index - 232),) * 3
    else:
        raise ValueError("预览只支持明确的 256 色索引")
    return "#" + "".join(f"{component:02x}" for component in rgb)


def cell_colors(screen: GridScreen, attribute: int) -> tuple[str, str]:
    foreground, background = screen.profile["foreground"], screen.profile["background"]
    pair = (attribute >> 8) & 255
    if pair in screen.color_pairs:
        fg_index, bg_index = screen.color_pairs[pair]
        foreground = foreground if fg_index == -1 else indexed_color(fg_index)
        background = background if bg_index == -1 else indexed_color(bg_index)
    if attribute & curses.A_REVERSE:
        foreground, background = background, foreground
    return foreground, background


def _svg(screen: GridScreen, *, title: str, description: str) -> str:
    width = LEFT_MARGIN * 2 + screen.width * CELL_WIDTH
    height = TOP_MARGIN + screen.height * CELL_HEIGHT + 24
    accessible_description = f"{description}\n\n{screen.plain_text()}"
    elements: list[str] = [
        '<!-- Generated by implementation/scripts/capture_tui_assets.py. -->',
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
            f'height="{height}" viewBox="0 0 {width} {height}">'
        ),
        f"  <title>{html.escape(title)}</title>",
        f"  <desc>{html.escape(accessible_description)}</desc>",
        (
            f'  <rect width="{width}" height="{height}" rx="16" '
            f'fill="{screen.profile["background"]}"/>'
        ),
        (
            f'  <rect width="{width}" height="44" rx="16" '
            f'fill="{screen.profile["chrome"]}"/>'
        ),
        f'  <rect y="28" width="100%" height="16" fill="{screen.profile["chrome"]}"/>',
        '  <circle cx="22" cy="22" r="6" fill="#ff5f57"/>',
        '  <circle cx="42" cy="22" r="6" fill="#febc2e"/>',
        '  <circle cx="62" cy="22" r="6" fill="#28c840"/>',
        (
            f'  <text x="{width / 2:.1f}" y="27" text-anchor="middle" '
            f'fill="{screen.profile["caption"]}" font-family="Menlo, Monaco, monospace" '
            f'font-size="14">{html.escape(title)}</text>'
        ),
        (
            f'  <g fill="{screen.profile["foreground"]}" '
            'font-family="Menlo, Monaco, Noto Sans Mono CJK SC, monospace" '
            'font-size="15">'
        ),
    ]
    for row_index, row in enumerate(screen.cells):
        baseline = TOP_MARGIN + (row_index + 1) * CELL_HEIGHT - 5
        column = 0
        while column < screen.width:
            _, background = cell_colors(screen, screen.attributes[row_index][column])
            end = column + 1
            while end < screen.width and cell_colors(screen, screen.attributes[row_index][end])[1] == background:
                end += 1
            if background != screen.profile["background"]:
                elements.append(f'    <rect x="{LEFT_MARGIN + column * CELL_WIDTH}" '
                                f'y="{TOP_MARGIN + row_index * CELL_HEIGHT}" '
                                f'width="{(end - column) * CELL_WIDTH}" '
                                f'height="{CELL_HEIGHT}" fill="{background}"/>')
            column = end
        for column, character in enumerate(row):
            if character in {None, "", " "}:
                continue
            x = LEFT_MARGIN + column * CELL_WIDTH
            attribute = screen.attributes[row_index][column]
            fill, _ = cell_colors(screen, attribute)
            weight = ' font-weight="bold"' if attribute & curses.A_BOLD else ""
            decoration = ' text-decoration="underline"' if attribute & curses.A_UNDERLINE else ""
            elements.append(
                f'    <text x="{x}" y="{baseline}" fill="{fill}"{weight}{decoration}>'
                f"{html.escape(character)}</text>"
            )
    elements.extend(("  </g>", "</svg>", ""))
    return "\n".join(elements)


def _clean_items():
    from openclean.models import Item

    return (
        Item(
            path=Path("/tmp/openclean-demo/pip"),
            size=5_400_000_000,
            category="pip / uv caches",
            safety="safe",
            preselected=True,
            domain="developer",
        ),
        Item(
            path=Path("/tmp/openclean-demo/homebrew"),
            size=1_700_000_000,
            category="Homebrew downloads",
            safety="safe",
            preselected=True,
            domain="developer",
        ),
        Item(
            path=Path("/tmp/openclean-demo/custom-cache"),
            size=860_000_000,
            category="环境变量缓存",
            safety="confirm",
            preselected=False,
            domain="developer",
            requires_explicit_selection=True,
        ),
        Item(
            path=None,
            size=12_800_000_000,
            category="Docker Local Volumes",
            safety="critical",
            preselected=False,
            domain="developer",
            actionable=False,
            action_block_reason="Local Volumes 永远拒绝 prune",
            resource_kind="docker",
            identifier="docker:volumes",
        ),
    )


def _render_clean_review(appearance: str = "dark", color_pairs=None) -> str:
    from openclean.tui import ReviewGroup, _draw_items, _item_key

    items = _clean_items()
    groups = (ReviewGroup("developer", "Dev Tools", items),)
    selected = {_item_key(item) for item in items if item.preselected}
    screen = GridScreen(appearance=appearance, color_pairs=color_pairs)
    _draw_items(
        screen,
        groups,
        group_index=0,
        cursor=2,
        selected_keys=selected,
        title="Clean · 扫描结果",
    )
    return _svg(
        screen,
        title="Clean TUI · 候选审阅",
        description=(
            "生产 Clean TUI 绘制逻辑使用固定合成候选生成的安全审阅画面。"
        ),
    )


def _render_clean_confirmation() -> str:
    from openclean.tui import ReviewGroup, _draw_confirmation, _item_key

    items = _clean_items()
    groups = (ReviewGroup("developer", "Dev Tools", items),)
    selected = {_item_key(item) for item in items if item.preselected}
    screen = GridScreen()
    _draw_confirmation(
        screen,
        groups,
        selected,
        title="Clean · 扫描结果",
        allow_execution=False,
    )
    return _svg(
        screen,
        title="Clean TUI · 只读汇总",
        description=(
            "未指定 --yes 时，生产 Clean TUI 明确说明只输出选择预览。"
        ),
    )


def _render_analyze_loading() -> str:
    from openclean.analyzer import AnalysisUpdate
    from openclean.models import Item
    from openclean.space_tui import _draw_loading

    root = Path("/tmp/openclean-demo/space")
    update = AnalysisUpdate(2, 8, (
        Item(root / "Projects", 31_000_000_000, "空间占用"),
        Item(root / "Archives", 7_800_000_000, "空间占用"),
    ))
    screen = GridScreen()
    _draw_loading(screen, root, update, elapsed=1.2)
    return _svg(screen, title="Analyze TUI · 扫描中",
                description="合成扫描进度；未知总量不显示精确百分比，Q 可取消。")


def _render_analyze() -> str:
    from openclean.analyzer import SpaceAnalysis, SpaceEntry
    from openclean.models import Item
    from openclean.space_tui import _draw_browser

    root = Path("/tmp/openclean-demo/space")
    items = (
        Item(
            path=root / "Projects",
            size=31_000_000_000,
            category="空间占用",
            safety="confirm",
            domain="analyze",
        ),
        Item(
            path=root / "Caches",
            size=13_500_000_000,
            category="空间占用",
            safety="confirm",
            domain="analyze",
        ),
        Item(
            path=root / "Archives",
            size=7_800_000_000,
            category="空间占用",
            safety="confirm",
            domain="analyze",
        ),
        Item(
            path=root / "Cloud-Placeholder",
            size=0,
            category="空间占用",
            safety="critical",
            domain="analyze",
            actionable=False,
            action_block_reason="dataless/疑似云占位",
            is_cloud_file=True,
            cloud_file_count=12,
        ),
        Item(
            path=root / "Logs",
            size=1_200_000_000,
            category="空间占用",
            safety="confirm",
            domain="analyze",
        ),
    )
    total = sum(item.size for item in items)
    entries = [
        SpaceEntry(item=item, percent=(item.size / total * 100 if total else 0))
        for item in items
    ]
    analysis = SpaceAnalysis(
        root=root,
        entries=entries,
        volume_total=1_000_000_000_000,
        volume_free=420_000_000_000,
        local_snapshots_checked=False,
    )
    selected = {items[1].path: items[1]}
    directories = {items[0].path, items[1].path, items[2].path}
    screen = GridScreen()
    with mock.patch(
        "openclean.space_tui._is_directory",
        side_effect=lambda path: path in directories,
    ):
        _draw_browser(
            screen,
            analysis,
            entries[:4],
            cursor=1,
            selected=selected,
            message="纯浏览不会修改文件；当前画面只使用固定合成数据。",
        )
    return _svg(
        screen,
        title="Analyze TUI · 空间浏览",
        description=(
            "生产 Analyze TUI 绘制逻辑使用固定合成目录生成的空间浏览画面。"
        ),
    )


def _render_menu() -> str:
    from openclean.tui import _draw_menu

    screen = GridScreen()
    _draw_menu(screen, "root", 0)
    return _svg(screen, title="OpenClean · 主菜单", description="生产主菜单绘制函数的固定预览，不执行清理。")


def _render_cli_scan(appearance: str = "dark") -> str:
    from openclean.cli import _print_report
    from openclean.models import ScanResult

    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        _print_report(ScanResult(items=list(_clean_items())), False, ["developer"])
    lines = output.getvalue().splitlines()
    screen = GridScreen(height=max(24, len(lines) + 2), appearance=appearance)
    for row, line in enumerate(lines):
        screen.addnstr(row, 1, line, len(line.encode("utf-8")))
    return _svg(screen, title="OpenClean CLI · 扫描报告", description="生产 CLI 文本报告，仅使用固定合成候选。")


@contextlib.contextmanager
def preview_styles(theme: str = "auto"):
    """调用生产初始化并记录真实的 pair 定义，不另造一套预览调色板。"""
    from openclean import terminal_ui

    pairs = {}
    with mock.patch.dict("os.environ", {"TERM": "xterm-256color", "OPENCLEAN_THEME": theme}, clear=True), \
            mock.patch.object(terminal_ui, "_colors_enabled", False), \
            mock.patch.object(terminal_ui, "_theme", "auto"), \
            mock.patch("curses.has_colors", return_value=True), \
            mock.patch("curses.start_color"), mock.patch("curses.use_default_colors"), \
            mock.patch("curses.COLORS", 256, create=True), \
            mock.patch("curses.init_pair", side_effect=lambda pair, fg, bg: pairs.update({pair: (fg, bg)})), \
            mock.patch("curses.color_pair", side_effect=lambda pair: pair << 8):
        terminal_ui.init_styles()
        yield pairs


def render_assets() -> dict[str, str]:
    with preview_styles():
        assets = {
            "tui-menu.svg": _render_menu(),
            "tui-clean-review.svg": _render_clean_review(),
            "tui-clean-confirm.svg": _render_clean_confirmation(),
            "tui-analyze.svg": _render_analyze(),
            "tui-analyze-loading.svg": _render_analyze_loading(),
            "cli-scan.svg": _render_cli_scan(),
            "tui-clean-review-light.svg": _render_clean_review("light"),
            "cli-scan-light.svg": _render_cli_scan("light"),
        }
    for appearance in ("light", "dark"):
        with preview_styles(appearance) as pairs:
            assets[f"tui-clean-review-colors-{appearance}.svg"] = _render_clean_review(appearance, pairs)
    if tuple(assets) != ASSET_NAMES:
        raise AssertionError("TUI 资产名称与清单不一致")
    forbidden = (
        "/Users/",
        "/Volumes/",
        "file://",
        "<script",
        "<foreignObject",
        "href=",
    )
    for name, content in assets.items():
        ET.fromstring(content)
        lowered = content.lower()
        for marker in forbidden:
            if marker.lower() in lowered:
                raise ValueError(f"{name} 包含禁止内容：{marker}")
    return assets


def _write_assets(output_dir: Path, assets: dict[str, str]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, content in assets.items():
        (output_dir / name).write_text(content, encoding="utf-8")
        print(f"wrote {output_dir / name}")


def _check_assets(output_dir: Path, assets: dict[str, str]) -> int:
    stale = []
    for name, content in assets.items():
        path = output_dir / name
        if not path.is_file() or path.read_text(encoding="utf-8") != content:
            stale.append(name)
    extras = sorted(
        path.name
        for path in output_dir.glob("tui-*.svg")
        if path.name not in assets
    ) if output_dir.is_dir() else []
    if stale or extras:
        if stale:
            print(
                "TUI 文档资产缺失或过期：" + ", ".join(stale),
                file=sys.stderr,
            )
        if extras:
            print("存在未管理的 TUI 资产：" + ", ".join(extras), file=sys.stderr)
        print(
            "运行 python3 implementation/scripts/capture_tui_assets.py --write",
            file=sys.stderr,
        )
        return 1
    print(f"TUI 文档资产：PASS（{len(assets)} 个确定性 SVG）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="生成或核对生产 TUI 的合成文档 SVG",
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--write", action="store_true", help="写入/更新资产")
    action.add_argument("--check", action="store_true", help="只读核对资产")
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"输出目录；默认 {DEFAULT_OUTPUT_DIR}",
    )
    args = parser.parse_args(argv)
    assets = render_assets()
    if args.write:
        _write_assets(args.output, assets)
        return 0
    return _check_assets(args.output, assets)


if __name__ == "__main__":
    raise SystemExit(main())
