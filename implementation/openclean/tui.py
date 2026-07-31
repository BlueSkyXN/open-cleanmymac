"""`clean` / `purge` 共用的 curses 分组复选审阅界面。"""
from __future__ import annotations

import curses
from dataclasses import dataclass

from .engine import human
from .models import Item


class TUIUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ReviewGroup:
    key: str
    label: str
    items: tuple[Item, ...]


@dataclass(frozen=True)
class ReviewResult:
    selected: tuple[Item, ...]
    submitted: bool
    execution_confirmed: bool
    cancelled: bool


def _item_key(item: Item) -> tuple[str, str, str, str]:
    location = str(item.path) if item.path is not None else item.identifier
    return item.resource_kind, location, item.domain, item.category


def _actionable(groups: tuple[ReviewGroup, ...]) -> list[Item]:
    return [
        item
        for group in groups
        for item in group.items
        if item.actionable and not item.requires_explicit_selection
    ]


def _selected_items(
    groups: tuple[ReviewGroup, ...],
    selected_keys: set[tuple[str, str, str, str]],
) -> tuple[Item, ...]:
    return tuple(
        item
        for group in groups
        for item in group.items
        if _item_key(item) in selected_keys
    )


def _safe_add(screen, row: int, column: int, text: str, width: int) -> None:
    if row < 0 or column >= width:
        return
    try:
        screen.addnstr(row, column, text, max(0, width - column - 1))
    except curses.error:
        pass


def _marker(item: Item, selected_keys: set) -> str:
    if not item.actionable:
        return "!"
    return "x" if _item_key(item) in selected_keys else " "


def _group_marker(group: ReviewGroup, selected_keys: set) -> str:
    actionable = [item for item in group.items if item.actionable]
    if not actionable:
        return "!"
    selected = sum(_item_key(item) in selected_keys for item in actionable)
    if selected == 0:
        return " "
    if selected == len(actionable):
        return "x"
    return "-"


def _draw_header(
    screen,
    title: str,
    groups: tuple[ReviewGroup, ...],
    selected_keys: set,
) -> int:
    height, width = screen.getmaxyx()
    selected = _selected_items(groups, selected_keys)
    _safe_add(screen, 0, 0, title, width)
    _safe_add(
        screen,
        1,
        0,
        f"已选 {len(selected)} 项 · {human(sum(item.size for item in selected))}",
        width,
    )
    if height > 2:
        _safe_add(screen, 2, 0, "─" * max(1, width - 1), width)
    return 3


def _draw_groups(
    screen,
    groups: tuple[ReviewGroup, ...],
    cursor: int,
    selected_keys: set,
    title: str,
) -> None:
    screen.erase()
    height, width = screen.getmaxyx()
    row = _draw_header(screen, title, groups, selected_keys)
    visible = max(1, height - row - 3)
    offset = max(0, min(cursor - visible + 1, len(groups) - visible))
    for index in range(offset, min(len(groups), offset + visible)):
        group = groups[index]
        prefix = "▸" if index == cursor else " "
        total = sum(item.size for item in group.items)
        line = (
            f"{prefix} [{_group_marker(group, selected_keys)}] "
            f"{group.label:<24} {human(total):>10}  →"
        )
        _safe_add(screen, row + index - offset, 0, line, width)
    footer = (
        "↑↓ 移动 · → 查看 · Space 切换分类 · "
        "A 批量选择 · Enter 确认 · Q 退出"
    )
    _safe_add(screen, height - 2, 0, footer, width)
    screen.refresh()


def _draw_items(
    screen,
    groups: tuple[ReviewGroup, ...],
    group_index: int,
    cursor: int,
    selected_keys: set,
    title: str,
) -> None:
    screen.erase()
    height, width = screen.getmaxyx()
    group = groups[group_index]
    row = _draw_header(
        screen,
        f"{title} / {group.label}",
        groups,
        selected_keys,
    )
    visible = max(1, height - row - 3)
    offset = max(0, min(cursor - visible + 1, len(group.items) - visible))
    for index in range(offset, min(len(group.items), offset + visible)):
        item = group.items[index]
        prefix = "▸" if index == cursor else " "
        location = str(item.path) if item.path is not None else item.identifier
        blocked = (
            f"  [不可执行: {item.action_block_reason}]"
            if not item.actionable
            else ""
        )
        exact = (
            "  [需逐项选择]"
            if item.actionable and item.requires_explicit_selection
            else ""
        )
        line = (
            f"{prefix} [{_marker(item, selected_keys)}] "
            f"{item.category:<24} {human(item.size):>10}  "
            f"{item.safety:<8} {location}{blocked}{exact}"
        )
        _safe_add(screen, row + index - offset, 0, line, width)
    footer = (
        "↑↓ 移动 · Space/Enter 逐项切换 · "
        "A 本组批量选择 · ←/Esc 返回 · Q 退出"
    )
    _safe_add(screen, height - 2, 0, footer, width)
    screen.refresh()


def _draw_confirmation(
    screen,
    groups: tuple[ReviewGroup, ...],
    selected_keys: set,
    title: str,
    allow_execution: bool,
) -> None:
    screen.erase()
    height, width = screen.getmaxyx()
    selected = _selected_items(groups, selected_keys)
    _safe_add(screen, 0, 0, f"{title} / 汇总确认", width)
    _safe_add(
        screen,
        2,
        0,
        f"选择 {len(selected)} 项，共 {human(sum(item.size for item in selected))}",
        width,
    )
    if allow_execution:
        message = "按 Y 确认执行；按 N/Esc 返回；按 Q 取消。"
    else:
        message = "本次未指定 --yes，只会输出选择预览。按 Enter 继续，Esc 返回。"
    _safe_add(screen, 4, 0, message, width)
    _safe_add(
        screen,
        height - 2,
        0,
        "普通项进入同卷 Trash；Trash/Docker prune 会永久释放空间。",
        width,
    )
    screen.refresh()


def _draw_critical_confirmation(screen, selected: tuple[Item, ...]) -> None:
    screen.erase()
    height, width = screen.getmaxyx()
    critical = [item for item in selected if item.safety == "critical"]
    _safe_add(screen, 0, 0, "Critical 二次确认", width)
    _safe_add(
        screen,
        2,
        0,
        f"已选择 {len(critical)} 个 critical 项。按 ! 执行；Esc 返回。",
        width,
    )
    _safe_add(screen, height - 2, 0, "此步骤不可由 --yes 单独绕过。", width)
    screen.refresh()


def _toggle_items(items: list[Item], selected_keys: set) -> None:
    actionable = [
        item
        for item in items
        if item.actionable and not item.requires_explicit_selection
    ]
    if not actionable:
        return
    all_selected = all(_item_key(item) in selected_keys for item in actionable)
    for item in actionable:
        key = _item_key(item)
        if all_selected:
            selected_keys.discard(key)
        else:
            selected_keys.add(key)


def _run_review(
    screen,
    groups: tuple[ReviewGroup, ...],
    *,
    title: str,
    allow_execution: bool,
) -> ReviewResult:
    screen.keypad(True)
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    selected_keys = {
        _item_key(item)
        for group in groups
        for item in group.items
        if (
            item.preselected is True
            and item.actionable
            and not item.requires_explicit_selection
        )
    }
    group_cursor = 0
    item_cursor = 0
    mode = "groups"

    while True:
        if mode == "groups":
            _draw_groups(screen, groups, group_cursor, selected_keys, title)
        elif mode == "items":
            _draw_items(
                screen,
                groups,
                group_cursor,
                item_cursor,
                selected_keys,
                title,
            )
        elif mode == "confirm":
            _draw_confirmation(
                screen,
                groups,
                selected_keys,
                title,
                allow_execution,
            )
        else:
            _draw_critical_confirmation(
                screen, _selected_items(groups, selected_keys)
            )

        key = screen.getch()
        if key in {ord("q"), ord("Q")}:
            return ReviewResult((), False, False, True)

        if mode == "groups":
            if key == curses.KEY_UP:
                group_cursor = max(0, group_cursor - 1)
            elif key == curses.KEY_DOWN:
                group_cursor = min(len(groups) - 1, group_cursor + 1)
            elif key in {curses.KEY_RIGHT, ord("l"), ord("L")}:
                item_cursor = 0
                mode = "items"
            elif key == ord(" "):
                _toggle_items(list(groups[group_cursor].items), selected_keys)
            elif key in {ord("a"), ord("A")}:
                _toggle_items(_actionable(groups), selected_keys)
            elif key in {curses.KEY_ENTER, 10, 13}:
                mode = "confirm"
            elif key == 27:
                return ReviewResult((), False, False, True)
            continue

        if mode == "items":
            items = groups[group_cursor].items
            if key == curses.KEY_UP:
                item_cursor = max(0, item_cursor - 1)
            elif key == curses.KEY_DOWN:
                item_cursor = min(len(items) - 1, item_cursor + 1)
            elif key in {curses.KEY_LEFT, ord("h"), ord("H"), 27}:
                mode = "groups"
            elif key in {ord(" "), curses.KEY_ENTER, 10, 13}:
                item = items[item_cursor]
                if item.actionable:
                    item_key = _item_key(item)
                    if item_key in selected_keys:
                        selected_keys.remove(item_key)
                    else:
                        selected_keys.add(item_key)
            elif key in {ord("a"), ord("A")}:
                _toggle_items(list(items), selected_keys)
            continue

        if mode == "confirm":
            selected = _selected_items(groups, selected_keys)
            if key in {27, ord("n"), ord("N")}:
                mode = "groups"
            elif not allow_execution and key in {
                curses.KEY_ENTER,
                10,
                13,
                ord("y"),
                ord("Y"),
            }:
                return ReviewResult(selected, True, False, False)
            elif allow_execution and key in {ord("y"), ord("Y")}:
                if any(item.safety == "critical" for item in selected):
                    mode = "critical"
                else:
                    return ReviewResult(selected, True, True, False)
            continue

        if key == 27:
            mode = "confirm"
        elif key == ord("!"):
            selected = _selected_items(groups, selected_keys)
            return ReviewResult(selected, True, True, False)


def review_cleanup(
    groups: tuple[ReviewGroup, ...],
    *,
    title: str,
    allow_execution: bool,
) -> ReviewResult:
    """打开 curses 审阅；空结果直接提交空选择。"""
    groups = tuple(group for group in groups if group.items)
    if not groups:
        return ReviewResult((), True, allow_execution, False)
    try:
        return curses.wrapper(
            lambda screen: _run_review(
                screen,
                groups,
                title=title,
                allow_execution=allow_execution,
            )
        )
    except (curses.error, OSError) as exc:
        raise TUIUnavailable(f"无法启动终端审阅界面：{exc}") from exc
