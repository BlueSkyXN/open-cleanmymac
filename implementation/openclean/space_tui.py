"""`analyze` 的 curses 空间浏览、复选与删除确认界面。"""
from __future__ import annotations

import curses
import os
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path

from .analyzer import AnalyzeError, SpaceAnalysis, SpaceEntry, analyze_path
from .engine import human
from .models import Item, ScanIssue, normalize_path
from .navigator import RevealError, reveal_in_finder
from .predicates import Predicate
from .scan_tui import ScanScreenFailure
from .space_session import AnalysisJob, SpaceCache
from .terminal_ui import (
    clip_cells, draw_footer, draw_small_screen, draw_text, init_styles,
    small_screen, wrap_cells, pad_cells,
)


class SpaceTUIUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class SpaceReviewResult:
    selected: tuple[Item, ...]
    submitted: bool
    execution_confirmed: bool
    cancelled: bool
    complete: bool = True
    issues: tuple[ScanIssue, ...] = ()


def _same_or_descendant(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((str(path), str(root))) == str(root)
    except ValueError:
        return False


def _safe_add(screen, row: int, column: int, text: str, width: int, role: str = "plain") -> None:
    draw_text(screen, row, column, text, width, role)


def _is_directory(path: Path) -> bool:
    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except OSError:
        return False


def _selected_total(selected: dict[Path, Item]) -> int:
    return sum(item.size for item in selected.values())


class SelectionIndex:
    """三态与范围汇总的派生显示数据；选择集合本身仍是唯一执行输入。

    按选择变化整体重建祖先聚合（O(选择数×路径深度)），绘制时只做
    O(1) 查表和 O(深度) 的上级覆盖检查，光标移动不触碰选择集合。
    """

    def __init__(self, selected: dict[Path, Item]):
        self.selected = selected
        self._inside: dict[Path, list[int]] = {}

    def rebuild(self) -> None:
        inside: dict[Path, list[int]] = {}
        for path, item in self.selected.items():
            for ancestor in path.parents:
                slot = inside.setdefault(ancestor, [0, 0])
                slot[0] += 1
                slot[1] += item.size
        self._inside = inside

    def inside(self, path: Path) -> tuple[int, int]:
        """严格位于 path 之内的显式选择目标数与既有计量。"""
        slot = self._inside.get(path)
        return (slot[0], slot[1]) if slot is not None else (0, 0)

    def covering_ancestor(self, path: Path) -> Path | None:
        """直接选择了 path 的某个祖先时返回该祖先，否则 None。"""
        return next(
            (ancestor for ancestor in path.parents if ancestor in self.selected),
            None,
        )

    def marker(self, item: Item) -> tuple[str, str]:
        """返回行的三态标记字符与附加说明（如“随上级”）。"""
        path = item.path
        if path is None or not item.actionable:
            return "!", ""
        if path in self.selected:
            return "x", ""
        if self.covering_ancestor(path) is not None:
            return "x", "随上级"
        if self.inside(path)[0]:
            return "-", ""
        return " ", ""

    def scope_summary(self, root: Path) -> str:
        """当前目录范围的选择汇总；被上级覆盖时不拆算虚构子项数量。"""
        if root in self.selected:
            return "此目录整体已选"
        if self.covering_ancestor(root) is not None:
            return "此目录包含在上级选择中"
        count, size = self.inside(root)
        if not count:
            return "此目录内没有已选目标"
        return f"此目录内已选 {count} 项 · {human(size)}"

    def focus_note(self, item: Item) -> str:
        """焦点说明：当前行最相关的选择范围或后果提示。"""
        path = item.path
        if path is None or not item.actionable:
            return item.action_block_reason or "该项不可执行"
        if path in self.selected:
            return "已直接选择此项。"
        ancestor = self.covering_ancestor(path)
        if ancestor is not None:
            return f"已包含在上级选择中：{ancestor}；如需调整请返回上级。"
        count, _ = self.inside(path)
        if count:
            return f"目录内有 {count} 个已选目标；Space 选择整个目录。"
        return ""


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
        return f"该项已由上级目录选择覆盖：{ancestor}；请返回上级调整。"
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
    entries = analysis.browse_entries if analysis.browse_entries is not None else analysis.entries
    return entries[:top] if top else entries


def _select_level(actionable, selected, index):
    """同层目标互不包含；批量合并一次，避免逐项遍历整个选择集。"""
    additions = {item.path: item for item in actionable
                 if item.path not in selected and index.covering_ancestor(item.path) is None}
    for path in tuple(selected):
        if any(parent in additions for parent in path.parents):
            del selected[path]
    selected.update(additions)
    return additions


def _partial_paths(analysis: SpaceAnalysis) -> set[Path]:
    """把 blocking issue 索引到当前根的一级子项，供同一页面重复绘制。"""
    root_parts = analysis.root.parts
    depth = len(root_parts)
    paths = set()
    for issue in analysis.issues:
        if issue.blocking and issue.path is not None:
            parts = issue.path.parts
            if len(parts) > depth and parts[:depth] == root_parts:
                paths.add(analysis.root / parts[depth])
    return paths


def _draw_browser(
    screen,
    analysis: SpaceAnalysis,
    entries: list[SpaceEntry],
    cursor: int,
    selected: dict[Path, Item],
    message: str,
    *,
    partial_paths: set[Path] | None = None,
    index: SelectionIndex | None = None,
) -> None:
    if partial_paths is None:
        partial_paths = _partial_paths(analysis)
    if index is None:
        index = SelectionIndex(selected)
        index.rebuild()
    screen.erase()
    height, width = screen.getmaxyx()
    _safe_add(screen, 0, 1, "Analyze · 空间浏览", width, "title")
    _safe_add(screen, 1, 1, clip_cells(analysis.root, width - 3, tail=True), width, "muted")
    volume = ""
    if analysis.volume_total is not None and analysis.volume_free is not None:
        volume = (
            f" · {human(analysis.volume_free)} 可用 / "
            f"{human(analysis.volume_total)} 总计"
        )
    if analysis.local_snapshots_checked:
        volume += f" · TM 本地快照 {len(analysis.local_snapshots)}"
    _safe_add(screen, 2, 1, f"全局已选 {len(selected)} 项 · {human(_selected_total(selected))}{volume}", width)
    status = ("分析不完整" if not analysis.complete else "占用不等于垃圾")
    if analysis.issues:
        status += f" · {len(analysis.issues)} 处提示"
    entry_count = len(_visible_entries(analysis, 0))
    if len(entries) < entry_count:
        status += f" · 显示最大 {len(entries)}/{entry_count} 项（占比基于全部）"
    _safe_add(screen, 3, 1, f"{index.scope_summary(analysis.root)} · {status}", width,
              "warning" if not analysis.complete else "muted")
    actionable = any(e.item.actionable for e in entries)
    actions = " · Space 选择 · A 全选" if actionable else " · 本页仅浏览"
    if actionable or selected:
        actions += " · Delete 汇总"
    footer = draw_footer(screen, "↑↓ 移动 · →/Enter 进入 · ← 返回 · R 刷新 · I 详情 · E 问题 · O Finder" + actions + " · Q 退出")
    visible = max(1, footer - 8)
    offset = max(0, min(cursor - visible + 1, len(entries) - visible))
    for index_row in range(offset, min(len(entries), offset + visible)):
        item = entries[index_row].item
        prefix = "▸" if index_row == cursor else " "
        marker_char, annotation = index.marker(item)
        if not item.actionable:
            state = "只读"
        elif annotation:
            state = f"[{marker_char}] {annotation}"
        else:
            state = f"[{marker_char}]"
        arrow = "→" if _is_directory(item.path) else " "
        cloud = (
            f" 云占位:{item.cloud_file_count}"
            if item.cloud_file_count
            else ""
        )
        filled = max(0, min(10, round(entries[index_row].percent / 10)))
        bar = ("━" * filled + "─" * (10 - filled) + "  ") if width >= 90 else ""
        partial = item.excluded_paths or item.path in partial_paths
        size = "未测" if partial and not item.size else human(item.size)
        name = item.path.name if item.path is not None else ""
        line = (f"{prefix} {pad_cells(state, 10)} {pad_cells(size, 10, right=True)} "
                f"{entries[index_row].percent:6.1f}%  {bar}{arrow} {name}{cloud}")
        role = "focus" if index_row == cursor else "warning" if not item.actionable else "plain"
        _safe_add(screen, 5 + index_row - offset, 1, line, width, role)
    if not entries:
        _safe_add(screen, 5, 1, "此目录没有可显示的项目。", width, "muted")
    elif entries[cursor].item.path is not None:
        _safe_add(screen, footer - 3, 1, clip_cells(entries[cursor].item.path, width - 3, tail=True), width, "muted")
    reason = index.focus_note(entries[cursor].item) if entries else ""
    _safe_add(screen, footer - 2, 1, reason, width, "warning" if reason else "muted")
    warning = next((issue.message for issue in analysis.issues if issue.blocking), "")
    _safe_add(screen, footer - 1, 1, message or warning, width,
              "warning" if warning and not message else "muted")
    screen.refresh()


def _draw_confirmation(
    screen,
    selected: dict[Path, Item],
    *,
    allow_execution: bool,
) -> None:
    screen.erase()
    height, width = screen.getmaxyx()
    title = "Analyze · 删除汇总" if allow_execution else "Analyze · 选择预览"
    _safe_add(screen, 0, 1, title, width, "title")
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
        message = "按 Y 确认移动到同卷废纸篓；Esc 返回；Q 取消。"
    else:
        message = "未指定 --yes，不会写文件。按 Enter 输出选择预览；Esc 返回。"
    draw_footer(screen, message)
    screen.refresh()


def _draw_critical_confirmation(screen, selected: dict[Path, Item]) -> None:
    screen.erase()
    height, width = screen.getmaxyx()
    count = sum(item.safety == "critical" for item in selected.values())
    _safe_add(screen, 0, 1, "Analyze · Critical 二次确认", width, "danger")
    _safe_add(
        screen,
        2,
        0,
        f"选择中包含 {count} 个 critical 项。按 ! 执行；Esc 返回。",
        width,
    )
    draw_footer(screen, "! 执行 · Esc 返回 · Q 取消；--yes 不能单独绕过此步骤。")
    screen.refresh()


def _scan_hints(pause_state: str) -> str:
    action = "Space 继续" if pause_state else "Space 暂停"
    return f"{action} · Q 取消审阅 · Ctrl-C 中断 · ← 返回；扫描期间不接受清理选择"


def _draw_loading(
    screen, path, update=None, elapsed=0.0, *, stopping=False, label="正在扫描",
    pause_state="",
) -> None:
    if small_screen(screen):
        draw_small_screen(screen)
        return
    screen.erase()
    height, width = screen.getmaxyx()
    state = (
        "正在停止" if stopping else
        "已暂停" if pause_state == "paused" else
        "暂停已请求" if pause_state == "requested" else label
    )
    _safe_add(screen, 0, 1, f"Analyze · {state}", width, "title")
    _safe_add(screen, 1, 1, clip_cells(path, width - 3, tail=True), width, "muted")
    status = f"已用 {elapsed:.1f} 秒；目录大小尚在计算"
    if pause_state == "requested":
        status += "；正在等待当前操作结束"
    if update is not None:
        status += f" · 已检查一级项 {update.completed}/{update.total}"
    _safe_add(screen, 3, 1, status, width)
    row = 5
    if update is not None and update.current_paths:
        prefix = "正在处理（之一）" if len(update.current_paths) > 1 else "正在处理"
        extra = f"（另 {len(update.current_paths) - 1} 批并行）" if len(update.current_paths) > 1 else ""
        _safe_add(
            screen, 4, 1,
            f"{prefix}：{clip_cells(update.current_paths[0], max(10, width - 30), tail=True)}{extra}",
            width, "muted",
        )
    footer = draw_footer(screen, _scan_hints(pause_state))
    if update is not None:
        for line_row, item in enumerate(update.entries[:max(0, footer - 6)], row):
            _safe_add(screen, line_row, 1, f"暂算 {human(item.size):>10}  {item.path.name}", width)
    screen.refresh()


def _pause_state(job) -> str:
    if job.paused:
        return "paused"
    if job.pause_requested:
        return "requested"
    return ""


def _load_analysis(screen, path, protection, analyzer, *, can_back=False, label="正在扫描",
                   on_scan_start=None):
    # 首帧不依赖路径访问、规则探测或工作线程调度。
    _draw_loading(screen, path, label=label)
    job = AnalysisJob(path, protection, analyzer)
    screen.timeout(50)
    drawn = 0.0
    started = False
    try:
        job.start()
        started = True
        if on_scan_start is not None:
            on_scan_start()
        while not job.done.is_set():
            now = time.monotonic()
            if now - drawn >= 0.1:
                _draw_loading(screen, path, job.update, now - job.started,
                              label=label, pause_state=_pause_state(job))
                drawn = now
            key = screen.getch()
            if key in {ord("q"), ord("Q")}:
                return None, "quit"
            if can_back and not small_screen(screen) and key in {curses.KEY_LEFT, curses.KEY_BACKSPACE, 8, 127}:
                return None, "back"
            if key == ord(" ") and not small_screen(screen) and not job.done.is_set():
                # 暂停/继续由实际检查点确认；完成与恢复竞争时完成优先。
                if job.pause_requested:
                    job.resume()
                else:
                    job.request_pause()
        if job.error is not None:
            raise job.error
        # 扫描转审阅的边界清理过期按键；Q 退出优先，Space/Enter 不泄漏进审阅页。
        screen.timeout(0)
        while True:
            stale = screen.getch()
            if stale == -1:
                break
            if stale in {ord("q"), ord("Q")}:
                return None, "quit"
        return job.result, "done"
    except (curses.error, OSError) as exc:
        if not started:
            raise
        raise ScanScreenFailure(f"分析界面失败：{exc}") from exc
    finally:
        try:
            if not job.done.is_set():
                try:
                    _draw_loading(screen, path, job.update, time.monotonic() - job.started, stopping=True)
                except (curses.error, OSError):
                    # 停止提示是尽力绘制；终端错误不能掩盖原有取消或中断。
                    pass
        finally:
            try:
                job.close()
            finally:
                screen.timeout(-1)


@dataclass
class _DetailLayout:
    source: list[str]
    width: int = 0
    wrapped: list[str] = field(default_factory=list)
    consumed: int = 0

    def page(self, width, offset, height):
        if width != self.width:
            self.width = width
            self.wrapped.clear()
            self.consumed = 0
        # 只排版当前窗口需要的文本，滚动复用；resize 后按新宽度重新排版。
        while self.consumed < len(self.source) and len(self.wrapped) < offset + height:
            self.wrapped.extend(wrap_cells(self.source[self.consumed], width))
            self.consumed += 1
        offset = min(offset, max(0, len(self.wrapped) - height))
        return offset, self.wrapped[offset:offset + height]


def _draw_details(screen, title, lines, offset, *, layout=None):
    screen.erase()
    height, width = screen.getmaxyx()
    _safe_add(screen, 0, 1, title, width, "title")
    footer = draw_footer(screen, "↑↓ 滚动 · Esc/← 返回 · Q 取消审阅")
    if layout is None:
        layout = _DetailLayout(lines)
    offset, visible = layout.page(max(1, width - 4), offset, max(1, footer - 3))
    for row, line in enumerate(visible, 2):
        _safe_add(screen, row, 1, line, width)
    screen.refresh()
    return offset


def _run_space_review(
    screen, start: Path, *, protection: Predicate, top: int, allow_execution: bool,
    analyzer=analyze_path, revealer=reveal_in_finder, on_scan_start=None,
) -> SpaceReviewResult:
    screen.keypad(True)
    init_styles()
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    current = normalize_path(start)
    history: list[tuple[Path, int]] = []
    selected: dict[Path, Item] = {}
    index = SelectionIndex(selected)
    index.rebuild()
    origins: dict[Path, Path] = {}
    cache = SpaceCache()
    cursor = 0
    message = "浏览/预览模式；不会修改文件。" if not allow_execution else "已允许执行；仍需选择和双重确认。"
    mode = "browse"
    analysis = None
    entries = []
    partial_paths = set()
    needs_analysis = True
    force = False
    complete = True
    issues: tuple[ScanIssue, ...] = ()
    details = []
    detail_layout = _DetailLayout(details)
    pending_notice = ""
    detail_title = ""
    detail_offset = 0

    def outcome(execution=False):
        return SpaceReviewResult(tuple(selected.values()), True, execution, False, complete, issues)

    while True:
        if small_screen(screen):
            draw_small_screen(screen)
            if screen.getch() in {ord("q"), ord("Q")}:
                return SpaceReviewResult((), False, False, True)
            continue
        if mode == "browse":
            if needs_analysis:
                view = None if force else cache.get(current)
                force = False
                if view is not None:
                    analysis = view.analysis
                    cursor = view.cursor
                    message = f"会话缓存（{time.monotonic() - view.observed_at:.0f} 秒前）；R 刷新，提交前重新复核。"
                else:
                    try:
                        analysis, action = _load_analysis(screen, current, protection, analyzer,
                                                         can_back=bool(history), on_scan_start=on_scan_start)
                    except AnalyzeError as exc:
                        pending_notice = str(exc)
                        if history:
                            current, cursor = history.pop()
                            continue
                        raise
                    if action == "quit":
                        return SpaceReviewResult((), False, False, True)
                    if action == "back":
                        current, cursor = history.pop()
                        continue
                    cache.put(analysis, cursor)
                if pending_notice:
                    message, pending_notice = pending_notice, ""
                entries = _visible_entries(analysis, top)
                partial_paths = _partial_paths(analysis)
                cursor = min(cursor, max(0, len(entries) - 1))
                needs_analysis = False
            _draw_browser(screen, analysis, entries, cursor, selected, message,
                          partial_paths=partial_paths, index=index)
        elif mode == "details":
            detail_offset = _draw_details(screen, detail_title, details, detail_offset,
                                          layout=detail_layout)
        elif mode == "confirm":
            _draw_confirmation(screen, selected, allow_execution=allow_execution)
        else:
            _draw_critical_confirmation(screen, selected)

        key = screen.getch()
        if key in {ord("q"), ord("Q")}:
            return SpaceReviewResult((), False, False, True)
        if mode == "details":
            if key in {27, curses.KEY_LEFT, curses.KEY_BACKSPACE, 8, 127}:
                mode = "browse"
            elif key == curses.KEY_DOWN:
                detail_offset += 1
            elif key == curses.KEY_UP:
                detail_offset = max(0, detail_offset - 1)
            continue
        if mode == "confirm":
            if key == 27:
                mode = "browse"
            elif not allow_execution and key in {curses.KEY_ENTER, 10, 13}:
                return outcome()
            elif allow_execution and key in {ord("y"), ord("Y")}:
                if any(item.safety == "critical" for item in selected.values()):
                    mode = "critical"
                else:
                    return outcome(True)
            continue
        if mode == "critical":
            if key == 27:
                mode = "confirm"
            elif key == ord("!"):
                return outcome(True)
            continue
        if key == curses.KEY_UP:
            cursor = max(0, cursor - 1)
        elif key == curses.KEY_DOWN:
            cursor = min(max(0, len(entries) - 1), cursor + 1)
        elif key in {curses.KEY_LEFT, curses.KEY_BACKSPACE, 8, 127}:
            if history:
                cache.remember_cursor(current, cursor)
                current, cursor = history.pop()
                needs_analysis = True
            else:
                message = "已经位于起始目录。"
        elif key in {curses.KEY_RIGHT, curses.KEY_ENTER, 10, 13}:
            if entries:
                path = entries[cursor].item.path
                if _is_directory(path):
                    cache.remember_cursor(current, cursor)
                    history.append((analysis.root, cursor))
                    current, cursor = path, 0
                    needs_analysis = True
                    if index.covering_ancestor(path) is not None:
                        message = "已包含在上级选择中；可继续查看，调整请返回上级。"
                    else:
                        message = ""
                else:
                    message = f"不是可进入的目录：{path}"
        elif key in {ord("r"), ord("R")}:
            cache.discard(current)
            needs_analysis = force = True
            message = "已刷新。"
        elif key == ord(" "):
            if entries:
                item = entries[cursor].item
                replaced, _ = index.inside(item.path) if item.path is not None else (0, 0)
                message = _toggle_item(item, selected)
                if item.path in selected:
                    origins[item.path] = analysis.root
                    if replaced:
                        message = f"已选择整个目录，替换 {replaced} 个子项选择。"
                index.rebuild()
        elif key in {ord("a"), ord("A")}:
            actionable = [entry.item for entry in entries if entry.item.actionable]
            if actionable and all(item.path in selected for item in actionable):
                for item in actionable:
                    selected.pop(item.path, None)
                message = "已取消当前层级选择。"
            else:
                added = _select_level(actionable, selected, index)
                origins.update((path, analysis.root) for path in added)
                message = "已选择当前层级可执行项。" if actionable else "本页仅可浏览。"
            index.rebuild()
        elif key in {ord("i"), ord("I")} and entries:
            item = entries[cursor].item
            details = [str(item.path), f"已分配：{human(item.size)}；风险：{item.safety}",
                       "状态：" + (item.action_block_reason or "可选择；执行前仍需复核"), item.note]
            detail_layout = _DetailLayout(details)
            detail_title, detail_offset, mode = "Analyze · 项目详情", 0, "details"
        elif key in {ord("e"), ord("E")}:
            details = [
                f"[{'问题' if issue.blocking else '提示'}:{issue.code}] "
                f"{issue.path or analysis.root}：{issue.message}"
                for issue in analysis.issues
            ]
            details = details or ["本次分析没有报告问题。"]
            detail_layout = _DetailLayout(details)
            detail_title, detail_offset, mode = "Analyze · 完整问题列表", 0, "details"
        elif key in {ord("o"), ord("O")} and entries:
            path = entries[cursor].item.path
            try:
                revealer(path)
            except RevealError as exc:
                message = f"Finder reveal 失败：{exc}"
            else:
                message = f"已在 Finder 中显示：{path}"
        elif key in {curses.KEY_DC, 330, ord("d"), ord("D")}:
            if not selected:
                message = "尚未选择任何项。"
                continue
            # 选择可能来自多个缓存页面；确认前刷新各来源，不用旧画面作为执行证据。
            complete = True
            collected_issues = []
            changed = []
            for root in dict.fromkeys(origins[path] for path in selected):
                try:
                    fresh, action = _load_analysis(screen, root, protection, analyzer,
                                                   label="复核选择", on_scan_start=on_scan_start)
                except AnalyzeError as exc:
                    fresh = None
                    action = "done"
                    collected_issues.append(ScanIssue("selection_changed", str(exc), "analyze", root))
                    complete = False
                if action == "quit":
                    return SpaceReviewResult((), False, False, True)
                candidates = {} if fresh is None else {entry.item.path: entry.item for entry in fresh.entries}
                if fresh is not None:
                    complete = complete and fresh.complete
                    collected_issues.extend(fresh.issues)
                    cache.put(fresh)
                    if root == current:
                        analysis = fresh
                        entries = _visible_entries(analysis, top)
                        partial_paths = _partial_paths(analysis)
                        cursor = min(cursor, max(0, len(entries) - 1))
                for path in list(selected):
                    if origins[path] != root:
                        continue
                    old = selected[path]
                    new = candidates.get(path)
                    if (new is None or not new.actionable or new.identity != old.identity
                            or new.size != old.size or new.logical_size != old.logical_size
                            or new.safety != old.safety):
                        selected.pop(path)
                        changed.append(path)
                    else:
                        selected[path] = new
            complete = complete and analysis.complete
            issues = tuple(dict.fromkeys([*collected_issues, *analysis.issues]))
            if changed:
                message = f"{len(changed)} 个选择已变化或不可执行，已撤销；请重新审阅。"
            elif selected:
                mode = "confirm"
            index.rebuild()
        # 不让已取消选择的来源记录无限增长。
        origins = {path: root for path, root in origins.items() if path in selected}


def review_space(
    start: Path,
    *,
    protection: Predicate,
    top: int = 0,
    allow_execution: bool,
) -> SpaceReviewResult:
    started = False

    def on_scan_start():
        nonlocal started
        started = True

    try:
        return curses.wrapper(
            lambda screen: _run_space_review(
                screen,
                start,
                protection=protection,
                top=top,
                allow_execution=allow_execution,
                on_scan_start=on_scan_start,
            )
        )
    except (curses.error, OSError) as exc:
        if started:
            raise ScanScreenFailure(f"分析界面失败：{exc}") from exc
        raise SpaceTUIUnavailable(
            f"无法启动空间浏览界面：{exc}"
        ) from exc
