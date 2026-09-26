"""主菜单及 `clean` / `purge` 共用的 curses 审阅界面。"""
from __future__ import annotations

import curses
from dataclasses import dataclass

from .engine import human
from .models import Item
from .terminal_ui import (
    clip_cells, draw_footer, draw_small_screen, draw_text, init_styles,
    pad_cells, safety_label, small_screen, wrap_cells,
)


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
    "more": (
        ("cheatsheet", "命令速查  常用 CLI 示例"),
        ("cat", "Cat       召唤一位朋友"),
        ("back", "返回主菜单"),
    ),
    "analyze_scope": (
        ("scope_home", "家目录"),
        ("scope_cwd", "当前目录"),
        ("scope_custom", "输入自定义目录"),
        ("scope_root", "启动盘 /（范围较大）"),
        ("back", "返回主菜单"),
    ),
    "optimize": (
        ("ram", "RAM        不可用：无已验证的安全公开执行器"),
        ("purgeable", "Purgeable  不可用：无已验证的安全公开执行器"),
        ("back", "返回主菜单"),
    ),
}
MENU_TITLES = {
    "root": "openclean · 主菜单（审阅/预览，不执行清理）",
    "more": "openclean · More",
    "analyze_scope": "Analyze · 选择分析范围",
    "optimize": "openclean · Optimize（Enter 查看原因，不执行维护）",
}

# 速查只展示可用命令示例，不自动拼接执行，不添加默认 --yes/--force。
CHEATSHEET_LINES = (
    ("人工文本 CLI（兼容期在 TTY 下加 --no-interactive）", (
        "openclean analyze . --no-interactive      分析当前目录",
        "openclean clean dev --no-interactive      查看开发缓存",
        "openclean purge . --no-interactive        预览项目产物",
    )),
    ("机器输出（JSON，适合脚本与 Agent）", (
        "openclean analyze . --json               输出 JSON",
    )),
    ("忽略项", (
        "openclean ignore list                   查看忽略项",
    )),
)
CHEATSHEET_NOTES = (
    "显式全屏界面：clean/purge/analyze 支持 --interactive（需连接终端）。",
    "执行清理仍需 --yes 与确认；完整参数见 openclean --help。",
)


@dataclass(frozen=True)
class MenuChoice:
    action: str
    cursor: int


def _draw_menu(screen, menu: str, cursor: int) -> None:
    screen.erase()
    height, width = screen.getmaxyx()
    _safe_add(screen, 0, 1, MENU_TITLES[menu], width, "title")
    _safe_add(screen, 1, 1, "选择一项任务开始 · 所有清理都先审阅", width, "muted")
    items = MENU_ITEMS[menu]
    step = 2 if height >= 20 else 1
    visible = max(1, (height - 7) // step)
    offset = max(0, cursor - visible + 1)
    for index in range(offset, min(len(items), offset + visible)):
        prefix = "▸" if index == cursor else " "
        _safe_add(screen, 4 + (index - offset) * step, 1,
                  f"{prefix} {items[index][1]}", width, "focus" if index == cursor else "plain")
    ending = "M 更多 · Q/Esc 退出" if menu == "root" else "Q/Esc 返回"
    draw_footer(screen, f"↑↓ 移动 · Enter 进入 · {ending} · 参数：openclean --help")
    screen.refresh()


def _draw_cheatsheet(screen) -> bool:
    """绘制命令速查；返回 True 表示用户按 Q 退出，False 表示返回 More。"""
    screen.erase()
    height, width = screen.getmaxyx()
    _safe_add(screen, 0, 1, "openclean · 命令速查", width, "title")
    row = 2
    for heading, examples in CHEATSHEET_LINES:
        if row >= height - len(CHEATSHEET_NOTES) - 3:
            break
        _safe_add(screen, row, 1, heading, width, "warning")
        row += 1
        for example in examples:
            if row >= height - len(CHEATSHEET_NOTES) - 3:
                break
            _safe_add(screen, row, 3, example, width)
            row += 1
        row += 1
    for note in CHEATSHEET_NOTES:
        _safe_add(screen, row, 1, note, width, "muted")
        row += 1
    draw_footer(screen, "Esc/Enter/← 返回 More · Q 退出")
    screen.refresh()
    key = screen.getch()
    return key in {ord("q"), ord("Q")}


def _run_menu(screen, *, menu: str, cursor: int) -> MenuChoice:
    screen.keypad(True)
    init_styles()
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    items = MENU_ITEMS[menu]
    cursor = max(0, min(cursor, len(items) - 1))
    while True:
        if small_screen(screen):
            draw_small_screen(screen)
            if screen.getch() in {ord("q"), ord("Q"), 27}:
                return MenuChoice("quit" if menu == "root" else "back", cursor)
            continue
        _draw_menu(screen, menu, cursor)
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
            if menu == "more" and items[cursor][0] == "cheatsheet":
                if _draw_cheatsheet(screen):
                    return MenuChoice("back", cursor)
                continue
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
    return wrap_cells(text, width)


def _draw_item_details(screen, item: Item, offset: int) -> tuple[int, int]:
    screen.erase()
    height, width = screen.getmaxyx()
    lines = [part for line in item_detail_lines(item)
             for part in _wrap_detail_line(line, max(1, width - 1))]
    footer = draw_footer(screen, "↑↓ 滚动 · Esc/← 返回 · Q 取消审阅")
    visible = max(1, footer - 3)
    maximum = max(0, len(lines) - visible)
    offset = max(0, min(offset, maximum))
    _safe_add(screen, 0, 1, "只读详情 · 不改变选择", width, "title")
    for row, line in enumerate(lines[offset:offset + visible], 2):
        _safe_add(screen, row, 0, line, width)
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


def _safe_add(screen, row: int, column: int, text: str, width: int, role: str = "plain") -> None:
    draw_text(screen, row, column, text, width, role)


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
    _, width = screen.getmaxyx()
    selected = _selected_items(groups, selected_keys)
    all_items = [item for group in groups for item in group.items]
    total = sum(item.size for item in all_items)
    actionable = sum(item.size for item in all_items if item.actionable)
    _safe_add(screen, 0, 1, title, width, "title")
    _safe_add(screen, 1, 1, f"发现 {human(total)} · 可操作 {human(actionable)}", width)
    _safe_add(screen, 2, 1, f"已选 {len(selected)} 项 · {human(sum(item.size for item in selected))}"
              f"  |  只读/阻断 {human(total - actionable)}", width, "muted")
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
    footer = draw_footer(screen, "↑↓ 移动 · → 查看 · Space 切换分类 · A 批量选择 · Enter 确认 · Q 退出")
    row += 1
    visible = max(1, footer - row - 1)
    offset = max(0, min(cursor - visible + 1, len(groups) - visible))
    for index in range(offset, min(len(groups), offset + visible)):
        group = groups[index]
        prefix = "▸" if index == cursor else " "
        total = sum(item.size for item in group.items)
        label_width = min(30, max(12, width - 36))
        line = (f"{prefix} [{_group_marker(group, selected_keys)}] "
                f"{pad_cells(clip_cells(group.label, label_width), label_width)} "
                f"{human(total):>10}  {len(group.items)} 项 →")
        _safe_add(screen, row + index - offset, 1, line, width, "focus" if index == cursor else "plain")
    screen.refresh()


def _group_scope_summary(group: ReviewGroup, selected_keys: set) -> str:
    """当前分类内已选对象的数量与既有计量。"""
    selected = [
        item for item in group.items if _item_key(item) in selected_keys
    ]
    if not selected:
        return "本分类暂无已选目标"
    return (
        f"本分类已选 {len(selected)} 项 · "
        f"{human(sum(item.size for item in selected))}"
    )


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
    footer = draw_footer(screen, "↑↓ 移动 · Space/Enter 切换 · I 只读详情 · A 批量选择 · ←/Esc 返回 · Q 退出")
    category_width = min(24, max(12, width // 5))
    _safe_add(screen, row, 1, _group_scope_summary(group, selected_keys), width, "muted")
    row += 1
    _safe_add(screen, row, 1, "      " + pad_cells("类别", category_width) + "       大小  状态 / 路径", width, "muted")
    row += 1
    visible = max(1, footer - row - 3)
    offset = max(0, min(cursor - visible + 1, len(group.items) - visible))
    for index in range(offset, min(len(group.items), offset + visible)):
        item = group.items[index]
        prefix = "▸" if index == cursor else " "
        location = str(item.path) if item.path is not None else item.identifier
        status = ("不可执行" if not item.actionable else
                  "需逐项选择" if item.requires_explicit_selection else
                  safety_label(item.safety))
        line = (
            f"{prefix} [{_marker(item, selected_keys)}] "
            f"{pad_cells(clip_cells(item.category, category_width), category_width)} {human(item.size):>10}  "
            f"{pad_cells(status, 10)} {location}"
        )
        role = "focus" if index == cursor else "warning" if not item.actionable else "selected" if _item_key(item) in selected_keys else "plain"
        _safe_add(screen, row + index - offset, 1, line, width, role)
    if group.items:
        current = group.items[cursor]
        location = current.path if current.path is not None else current.identifier
        _safe_add(screen, footer - 3, 1, clip_cells(location, width - 3, tail=True), width, "muted")
        state = (f"不可执行：{current.action_block_reason}" if not current.actionable else
                 "需逐项选择 · 批量选择不会包含此项" if current.requires_explicit_selection else
                 f"{safety_label(current.safety)} · I 查看说明与完整路径")
        _safe_add(screen, footer - 2, 1, state, width, "warning" if not current.actionable else "plain")
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
    heading = f"{title} / 选择预览" if not allow_execution else f"{title} / 汇总确认"
    _safe_add(screen, 0, 1, heading, width, "title")
    _safe_add(
        screen,
        2,
        1,
        f"选择 {len(selected)} 项，共 {human(sum(item.size for item in selected))}",
        width,
    )
    if allow_execution:
        message = "按 Y 确认执行；按 N/Esc 返回；按 Q 取消。"
    else:
        message = "本次未指定 --yes，只会输出选择预览。按 Enter 继续，Esc 返回。"
    footer = draw_footer(screen, message)
    warnings = [line for warning in ("普通项进入同卷 Trash。", "Trash/Docker prune 是永久删除操作。")
                for line in wrap_cells(warning, width - 2)]
    warning_start = footer - 1 - len(warnings)
    available = max(0, warning_start - 5)
    for row, item in enumerate(selected[:available], 4):
        _safe_add(screen, row, 1, f"{human(item.size):>10}  {item.path or item.identifier}", width)
    if len(selected) > available:
        _safe_add(screen, warning_start - 1, 1, f"另 {len(selected) - available} 项；返回列表可逐项检查", width, "muted")
    for row, line in enumerate(warnings, warning_start):
        _safe_add(screen, row, 1, line, width, "warning")
    _safe_add(screen, footer - 1, 1, "移动到废纸篓不等于已释放空间。", width, "muted")
    screen.refresh()


def _draw_critical_confirmation(screen, selected: tuple[Item, ...]) -> None:
    screen.erase()
    height, width = screen.getmaxyx()
    critical = [item for item in selected if item.safety == "critical"]
    _safe_add(screen, 0, 1, "Critical 二次确认", width, "danger")
    _safe_add(
        screen,
        2,
        0,
        f"已选择 {len(critical)} 个 critical 项。按 ! 执行；Esc 返回。",
        width,
    )
    draw_footer(screen, "! 执行 · Esc 返回 · Q 取消；此步骤不可由 --yes 单独绕过。")
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
    init_styles()
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
        if small_screen(screen):
            draw_small_screen(screen)
            if screen.getch() in {ord("q"), ord("Q")}:
                return ReviewResult((), False, False, True)
            continue
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
