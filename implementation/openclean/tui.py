"""主菜单及 `clean` / `purge` 共用的 curses 审阅界面。"""
from __future__ import annotations

import curses
import unicodedata
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


MENU_ITEMS = {
    "root": (
        ("clean", "Clean     扫描并审阅垃圾"),
        ("purge", "Purge     查找开发产物"),
        ("analyze", "Analyze   分析磁盘空间"),
        ("optimize", "Optimize  查看维护能力（当前不可用）"),
        ("config", "Config    查看 CLI 偏好"),
    ),
    "more": (("cat", "Cat       召唤一位朋友"), ("back", "返回主菜单")),
    "optimize": (
        ("ram", "RAM        不可用：无已验证的安全公开执行器"),
        ("purgeable", "Purgeable  不可用：无已验证的安全公开执行器"),
        ("back", "返回主菜单"),
    ),
}
MENU_TITLES = {
    "root": "openclean · 主菜单（审阅/预览，不执行清理）",
    "more": "openclean · More",
    "optimize": "openclean · Optimize（Enter 查看原因，不执行维护）",
}


@dataclass(frozen=True)
class MenuChoice:
    action: str
    cursor: int


def _run_menu(screen, *, menu: str, cursor: int) -> MenuChoice:
    screen.keypad(True)
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    items = MENU_ITEMS[menu]
    cursor = max(0, min(cursor, len(items) - 1))
    while True:
        screen.erase()
        height, width = screen.getmaxyx()
        _safe_add(screen, 0, 0, MENU_TITLES[menu], width)
        _safe_add(screen, 1, 0, "完整命令与参数：openclean --help", width)
        visible = max(1, height - 5)
        offset = max(0, cursor - visible + 1)
        for index in range(offset, min(len(items), offset + visible)):
            prefix = "▸" if index == cursor else " "
            _safe_add(screen, 3 + index - offset, 0,
                      f"{prefix} {items[index][1]}", width)
        ending = "M 更多 · Q/Esc 退出" if menu == "root" else "Q/Esc 返回"
        _safe_add(screen, height - 1, 0, f"↑↓ 移动 · Enter 进入 · {ending}", width)
        screen.refresh()
        key = screen.getch()
        if key in {ord("q"), ord("Q"), 27}:
            return MenuChoice("quit" if menu == "root" else "back", cursor)
        if menu == "root" and key in {ord("m"), ord("M")}:
            return MenuChoice("more", cursor)
        if key == curses.KEY_UP:
            cursor = max(0, cursor - 1)
        elif key == curses.KEY_DOWN:
            cursor = min(len(items) - 1, cursor + 1)
        elif key in {curses.KEY_ENTER, 10, 13}:
            return MenuChoice(items[cursor][0], cursor)


def choose_menu(menu: str, cursor: int = 0) -> MenuChoice:
    """只返回选择；wrapper 恢复终端后才由 CLI 调用子命令。"""
    try:
        return curses.wrapper(lambda screen: _run_menu(screen, menu=menu, cursor=cursor))
    except (curses.error, OSError) as exc:
        raise TUIUnavailable(f"无法启动主菜单：{exc}") from exc


def _count(value: int | None) -> str:
    return "未知" if value is None else str(value)


def _size(value: int | None) -> str:
    return "未知" if value is None else human(value)


def item_diagnostic_summary(item: Item) -> tuple[str, ...]:
    """只格式化已有证据；同时供详情页和 Clean 文本报告使用。"""
    lines: list[str] = []
    if item.diagnostic_kind == "retention":
        lines.extend((
            f"文件数：{_count(item.retention_file_count)}",
            "7/14/30 天以上占用（重叠桶，不累加）："
            f"{_size(item.retention_7d_bytes)} / {_size(item.retention_14d_bytes)} / "
            f"{_size(item.retention_30d_bytes)}",
        ))
    elif item.diagnostic_kind == "sqlite_freelist":
        ratio = ("未知" if item.sqlite_internal_free_ratio is None
                 else f"{item.sqlite_internal_free_ratio:.1%}")
        lines.extend((
            f"页大小：{_size(item.sqlite_page_size)}；总页/空闲页："
            f"{_count(item.sqlite_page_count)} / {_count(item.sqlite_freelist_count)}",
            f"内部空闲：{_size(item.sqlite_internal_free_bytes)}（{ratio}）；"
            f"WAL：{_size(item.sqlite_wal_bytes)}；不等于可直接回收空间",
        ))
    elif item.diagnostic_kind == "codex_transient":
        complete = {True: "完整", False: "不完整", None: "未知"}[item.measurement_complete]
        lines.append(f"总数/已测：{_count(item.total_count)} / {_count(item.measured_count)}；"
                     f"测量完整性：{complete}")
    elif item.diagnostic_kind == "crashpad_pairing":
        lines.append(f"总数/配对/近期：{_count(item.total_count)} / "
                     f"{_count(item.paired_artifact_count)} / {_count(item.recent_artifact_count)}")
    elif item.diagnostic_kind == "open_unlinked":
        lines.append(f"逻辑大小上限：{_size(item.logical_size)}（非可回收量）；"
                     f"对象/相关进程：{_count(item.total_count)} / {_count(item.related_process_count)}")
    if item.updater_status or item.diagnostic_kind == "updater_temp":
        lines.append(f"更新状态：{item.updater_status or '未知'}；"
                     f"已安装版本：{item.installed_version or '未知'}；"
                     f"暂存版本：{item.staged_version or '未知'}")
    return tuple(lines)


def item_detail_lines(item: Item) -> tuple[str, ...]:
    state = ("只读诊断（不可执行）" if item.diagnostic_kind else
             "可清理候选（不等于已授权）" if item.actionable else "被保护或阻断")
    lines = [
        f"状态：{state}",
        f"路径 / identifier：{item.path if item.path is not None else item.identifier}",
        f"分类：{item.category}；域：{item.domain or '未知'}；风险：{item.safety}",
        f"占用：{human(item.size)}（不等于已释放空间）",
        f"说明：{item.note or '未知'}",
        f"年龄（天）：{_count(item.age_days)}；打开句柄数：{_count(item.open_handle_count)}",
        f"阻断原因：{item.action_block_reason or ('无' if item.actionable else '未知')}",
    ]
    if item.requires_explicit_selection:
        lines.append("选择限制：需要逐项精确选择")
    if item.requires_privilege:
        lines.append("权限限制：需要尚未实现的特权 helper")
    if item.resource_kind == "filesystem_subset":
        lines.append("此路径仅为命中子集的聚合锚点，不代表整个父目录可处理。")
    lines.extend(item_diagnostic_summary(item))
    return tuple(lines)


def _wrap_detail_line(text: str, width: int) -> list[str]:
    # 按终端列宽折行，完整保留长路径；控制字符以转义形式显示。
    text = "".join(char if char.isprintable() else repr(char)[1:-1] for char in text)
    lines: list[str] = []
    current = ""
    columns = 0
    for char in text:
        size = (0 if unicodedata.combining(char) else
                2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1)
        if current and columns + size > max(1, width):
            lines.append(current)
            current, columns = "", 0
        current += char
        columns += size
    lines.append(current)
    return lines


def _draw_item_details(screen, item: Item, offset: int) -> tuple[int, int]:
    screen.erase()
    height, width = screen.getmaxyx()
    lines = [part for line in item_detail_lines(item)
             for part in _wrap_detail_line(line, max(1, width - 1))]
    visible = max(1, height - 4)
    maximum = max(0, len(lines) - visible)
    offset = max(0, min(offset, maximum))
    _safe_add(screen, 0, 0, "只读详情 · 不改变选择", width)
    for row, line in enumerate(lines[offset:offset + visible], 2):
        _safe_add(screen, row, 0, line, width)
    _safe_add(screen, height - 1, 0, "↑↓ 滚动 · Esc/← 返回 · Q 取消审阅", width)
    screen.refresh()
    return offset, maximum


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
        "↑↓ 移动 · Space/Enter 切换 · "
        "I 只读详情 · A 批量选择 · ←/Esc 返回 · Q 退出"
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
    detail_offset = 0
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
        elif mode == "details":
            detail_offset, detail_maximum = _draw_item_details(
                screen, groups[group_cursor].items[item_cursor], detail_offset
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
            elif key in {ord("i"), ord("I")}:
                detail_offset = 0
                mode = "details"
            continue

        if mode == "details":
            if key == curses.KEY_UP:
                detail_offset = max(0, detail_offset - 1)
            elif key == curses.KEY_DOWN:
                detail_offset = min(detail_maximum, detail_offset + 1)
            elif key in {curses.KEY_LEFT, 27}:
                mode = "items"
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
