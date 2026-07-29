"""`analyze` 的 curses 空间浏览、复选与删除确认界面。"""
from __future__ import annotations

import curses
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .analyzer import AnalyzeError, SpaceAnalysis, SpaceEntry, analyze_path
from .engine import Control, human
from .models import Item
from .navigator import RevealError, reveal_in_finder
from .predicates import Predicate


class SpaceTUIUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class SpaceReviewResult:
    selected: tuple[Item, ...]
    submitted: bool
    execution_confirmed: bool
    cancelled: bool


def _same_or_descendant(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((str(path), str(root))) == str(root)
    except ValueError:
        return False


def _safe_add(screen, row: int, column: int, text: str, width: int) -> None:
    if row < 0 or column >= width:
        return
    try:
        screen.addnstr(row, column, text, max(0, width - column - 1))
    except curses.error:
        pass


def _is_directory(path: Path) -> bool:
    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except (PermissionError, FileNotFoundError, OSError):
        return False


def _selected_total(selected: dict[Path, Item]) -> int:
    return sum(item.size for item in selected.values())


def _toggle_item(item: Item, selected: dict[Path, Item]) -> str:
    if item.path is None or not item.actionable:
        return item.action_block_reason or "该项不可执行"
    path = item.path
    if path in selected:
        del selected[path]
        return f"已取消选择：{path}"
    ancestor = next(
        (
            existing
            for existing in selected
            if existing != path and _same_or_descendant(path, existing)
        ),
        None,
    )
    if ancestor is not None:
        return f"已选择上级目录，不能重复选择：{ancestor}"
    descendants = [
        existing
        for existing in selected
        if existing != path and _same_or_descendant(existing, path)
    ]
    for descendant in descendants:
        del selected[descendant]
    selected[path] = item
    return f"已选择：{path}"


def _visible_entries(analysis: SpaceAnalysis, top: int) -> list[SpaceEntry]:
    return analysis.entries[:top] if top else analysis.entries


def _draw_browser(
    screen,
    analysis: SpaceAnalysis,
    entries: list[SpaceEntry],
    cursor: int,
    selected: dict[Path, Item],
    message: str,
) -> None:
    screen.erase()
    height, width = screen.getmaxyx()
    _safe_add(screen, 0, 0, f"Analyze · {analysis.root}", width)
    volume = ""
    if analysis.volume_total is not None and analysis.volume_free is not None:
        volume = (
            f" · {human(analysis.volume_free)} 可用 / "
            f"{human(analysis.volume_total)} 总计"
        )
    if analysis.local_snapshots_checked:
        volume += f" · TM 本地快照 {len(analysis.local_snapshots)}"
    _safe_add(
        screen,
        1,
        0,
        f"已选 {len(selected)} 项 · {human(_selected_total(selected))}{volume}",
        width,
    )
    _safe_add(screen, 2, 0, "─" * max(1, width - 1), width)
    visible = max(1, height - 7)
    offset = max(0, min(cursor - visible + 1, len(entries) - visible))
    for index in range(offset, min(len(entries), offset + visible)):
        item = entries[index].item
        prefix = "▸" if index == cursor else " "
        if not item.actionable:
            marker = "!"
        else:
            marker = "x" if item.path in selected else " "
        arrow = "→" if _is_directory(item.path) else " "
        cloud = (
            f" 云占位:{item.cloud_file_count}"
            if item.cloud_file_count
            else ""
        )
        line = (
            f"{prefix} [{marker}] {human(item.size):>10} "
            f"{entries[index].percent:6.1f}%  {arrow} {item.path}{cloud}"
        )
        _safe_add(screen, 3 + index - offset, 0, line, width)
    _safe_add(screen, height - 3, 0, message, width)
    _safe_add(
        screen,
        height - 2,
        0,
        "↑↓ 移动 · →/Enter 进入 · ← 返回 · Space 选择 · A 全选 · "
        "O Finder · Delete 汇总 · Q 退出",
        width,
    )
    screen.refresh()


def _draw_confirmation(
    screen,
    selected: dict[Path, Item],
    *,
    allow_execution: bool,
) -> None:
    screen.erase()
    height, width = screen.getmaxyx()
    _safe_add(screen, 0, 0, "Analyze · 删除汇总", width)
    _safe_add(
        screen,
        2,
        0,
        f"选择 {len(selected)} 项，共 {human(_selected_total(selected))}",
        width,
    )
    row = 4
    for path in list(selected)[: max(0, height - 8)]:
        _safe_add(screen, row, 2, str(path), width)
        row += 1
    if allow_execution:
        message = "按 Y 确认移动到同卷 Trash；Esc 返回；Q 取消。"
    else:
        message = "未指定 --yes，不会写文件。按 Enter 输出选择预览；Esc 返回。"
    _safe_add(screen, height - 2, 0, message, width)
    screen.refresh()


def _draw_critical_confirmation(screen, selected: dict[Path, Item]) -> None:
    screen.erase()
    height, width = screen.getmaxyx()
    count = sum(item.safety == "critical" for item in selected.values())
    _safe_add(screen, 0, 0, "Analyze · Critical 二次确认", width)
    _safe_add(
        screen,
        2,
        0,
        f"选择中包含 {count} 个 critical 项。按 ! 执行；Esc 返回。",
        width,
    )
    _safe_add(screen, height - 2, 0, "--yes 不能单独绕过此步骤。", width)
    screen.refresh()


def _run_space_review(
    screen,
    start: Path,
    *,
    protection: Predicate,
    top: int,
    allow_execution: bool,
    analyzer=analyze_path,
    revealer=reveal_in_finder,
) -> SpaceReviewResult:
    screen.keypad(True)
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    current = start.expanduser()
    history: list[Path] = []
    selected: dict[Path, Item] = {}
    cursor = 0
    message = "纯浏览不会修改文件。"
    mode = "browse"
    control = Control()
    analysis: SpaceAnalysis | None = None
    entries: list[SpaceEntry] = []
    needs_analysis = True

    while True:
        if mode == "browse":
            if needs_analysis:
                try:
                    analysis = analyzer(
                        current,
                        protection=protection,
                        control=control,
                    )
                except AnalyzeError as exc:
                    message = str(exc)
                    if history:
                        current = history.pop()
                        cursor = 0
                        needs_analysis = True
                        continue
                    raise
                entries = _visible_entries(analysis, top)
                cursor = min(cursor, max(0, len(entries) - 1))
                needs_analysis = False
            _draw_browser(screen, analysis, entries, cursor, selected, message)
        elif mode == "confirm":
            _draw_confirmation(
                screen, selected, allow_execution=allow_execution
            )
        else:
            _draw_critical_confirmation(screen, selected)

        key = screen.getch()
        if key in {ord("q"), ord("Q")}:
            return SpaceReviewResult((), False, False, True)

        if mode == "confirm":
            if key == 27:
                mode = "browse"
            elif not allow_execution and key in {
                curses.KEY_ENTER,
                10,
                13,
            }:
                return SpaceReviewResult(
                    tuple(selected.values()), True, False, False
                )
            elif allow_execution and key in {ord("y"), ord("Y")}:
                if any(
                    item.safety == "critical" for item in selected.values()
                ):
                    mode = "critical"
                else:
                    return SpaceReviewResult(
                        tuple(selected.values()), True, True, False
                    )
            continue
        if mode == "critical":
            if key == 27:
                mode = "confirm"
            elif key == ord("!"):
                return SpaceReviewResult(
                    tuple(selected.values()), True, True, False
                )
            continue

        if key == curses.KEY_UP:
            cursor = max(0, cursor - 1)
            continue
        if key == curses.KEY_DOWN:
            cursor = min(max(0, len(entries) - 1), cursor + 1)
            continue
        if key in {curses.KEY_LEFT, curses.KEY_BACKSPACE, 8, 127}:
            if history:
                current = history.pop()
                cursor = 0
                needs_analysis = True
                message = "已返回上一级。"
            else:
                message = "已经位于起始目录。"
            continue
        if key in {curses.KEY_RIGHT, curses.KEY_ENTER, 10, 13}:
            if not entries:
                continue
            path = entries[cursor].item.path
            if _is_directory(path):
                history.append(analysis.root)
                current = path
                cursor = 0
                needs_analysis = True
                message = ""
            else:
                message = f"不是可进入的目录：{path}"
            continue
        if key in {ord("r"), ord("R")}:
            needs_analysis = True
            message = "已刷新。"
            continue
        if key == ord(" "):
            if entries:
                message = _toggle_item(entries[cursor].item, selected)
            continue
        if key in {ord("a"), ord("A")}:
            actionable = [entry.item for entry in entries if entry.item.actionable]
            if actionable and all(item.path in selected for item in actionable):
                for item in actionable:
                    selected.pop(item.path, None)
                message = "已取消当前层级选择。"
            else:
                for item in actionable:
                    if item.path not in selected:
                        _toggle_item(item, selected)
                message = "已选择当前层级可执行项。"
            continue
        if key in {ord("o"), ord("O")}:
            if not entries:
                continue
            path = entries[cursor].item.path
            try:
                revealer(path)
            except RevealError as exc:
                message = f"Finder reveal 失败：{exc}"
            else:
                message = f"已在 Finder 中显示：{path}"
            continue
        if key in {curses.KEY_DC, 330, ord("d"), ord("D")}:
            if selected:
                mode = "confirm"
            else:
                message = "尚未选择任何项。"


def review_space(
    start: Path,
    *,
    protection: Predicate,
    top: int = 0,
    allow_execution: bool,
) -> SpaceReviewResult:
    try:
        return curses.wrapper(
            lambda screen: _run_space_review(
                screen,
                start,
                protection=protection,
                top=top,
                allow_execution=allow_execution,
            )
        )
    except (curses.error, OSError) as exc:
        raise SpaceTUIUnavailable(
            f"无法启动空间浏览界面：{exc}"
        ) from exc
