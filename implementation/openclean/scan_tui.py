"""clean/purge 与 analyze 共用的扫描阶段组件：扫描会话与统一扫描界面。

工作线程只扫描，主线程负责 curses；复用 Analyze 已验证的生命周期：
最新进度快照与结果/异常通道分开，取消后等待 worker 自己发出 done，
再回收线程。本模块不改变选择语义、执行资格或 JSON 输出。
"""
from __future__ import annotations

import curses
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass

from .engine import Cancelled, Control
from .progress import ProgressSnapshot, TaskProgressSnapshot
from .terminal_ui import (
    clip_cells, draw_footer, draw_small_screen, draw_text, init_styles,
    small_screen,
)
from .tui import TUIUnavailable


class PauseObservableControl(Control):
    """在真正进入暂停等待时确认；只服务扫描界面的暂停状态显示。

    “已暂停”必须由实际任务到达暂停点反馈；阻塞 I/O 尚未到达检查点时
    界面只能显示“暂停已请求”，不得凭按键状态宣称已停住。
    """

    def __init__(self) -> None:
        super().__init__()
        self.pause_confirmed = threading.Event()
        self._activity_lock = threading.Lock()
        self._active: dict[int, int] = {}
        self._waiting: set[int] = set()

    def _refresh_pause(self):
        confirmed = (not self._pause.is_set() and bool(self._active)
                     and self._active.keys() <= self._waiting)
        if confirmed:
            self.pause_confirmed.set()
        else:
            self.pause_confirmed.clear()

    def pause(self):
        with self._activity_lock:
            super().pause()
            self._refresh_pause()

    def resume(self):
        with self._activity_lock:
            super().resume()
            self._refresh_pause()

    def cancel(self):
        with self._activity_lock:
            super().cancel()
            self._refresh_pause()

    def run_task(self, action, *args, **kwargs):
        identity = threading.get_ident()
        with self._activity_lock:
            self._active[identity] = self._active.get(identity, 0) + 1
            self._refresh_pause()
        try:
            self.checkpoint()
            return action(*args, **kwargs)
        finally:
            with self._activity_lock:
                self._active[identity] -= 1
                if not self._active[identity]:
                    del self._active[identity]
                self._waiting.discard(identity)
                self._refresh_pause()

    @contextmanager
    def delegating(self):
        identity = threading.get_ident()
        with self._activity_lock:
            suspended = self._active.pop(identity, 0)
            self._refresh_pause()
        try:
            yield
        finally:
            with self._activity_lock:
                if suspended:
                    self._active[identity] = suspended
                self._refresh_pause()

    def checkpoint(self) -> None:
        identity = threading.get_ident()
        while True:
            with self._activity_lock:
                if self._pause.is_set():
                    self._waiting.discard(identity)
                    self._refresh_pause()
                    if self._cancel.is_set():
                        raise Cancelled()
                    return
                self._waiting.add(identity)
                self._refresh_pause()
            self._pause.wait()


class ScanScreenFailure(RuntimeError):
    """扫描已开始后的界面失败；worker 已取消并收尾，不静默重开扫描。"""


@dataclass(frozen=True)
class ScanScreenOutcome:
    status: str  # "done" | "cancelled"
    result: object | None = None
    error: BaseException | None = None


class ScanJob:
    """一次性扫描线程封装：``action(control, on_progress) -> result``。"""

    def __init__(self, action):
        self.control = PauseObservableControl()
        self.pause_requested = False
        self.started = time.monotonic()
        self.done = threading.Event()
        self.result = None
        self.error: BaseException | None = None
        self._lock = threading.Lock()
        self._snapshot: ProgressSnapshot | None = None

        def publish(snapshot: ProgressSnapshot) -> None:
            with self._lock:
                self._snapshot = snapshot

        def run():
            try:
                self.result = self.control.run_task(action, self.control, publish)
            except BaseException as exc:
                self.error = exc
            finally:
                self.done.set()

        self._action = run
        self._pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="cleanup-scan",
        )
        self._future = None

    def start(self):
        self._future = self._pool.submit(self._action)

    def request_pause(self) -> None:
        self.pause_requested = True
        self.control.pause_confirmed.clear()
        self.control.pause()

    def resume(self) -> None:
        self.pause_requested = False
        self.control.pause_confirmed.clear()
        self.control.resume()

    @property
    def paused(self) -> bool:
        return self.pause_requested and self.control.pause_confirmed.is_set()

    @property
    def snapshot(self) -> ProgressSnapshot | None:
        with self._lock:
            return self._snapshot

    def close(self):
        self.control.cancel()
        # 与 AnalysisJob 相同：先等 worker 自己发出的完成信号，再回收线程。
        while True:
            try:
                self._pool.shutdown(wait=False, cancel_futures=True)
                if self._future is not None and not self._future.cancelled():
                    self.done.wait()
                self._pool.shutdown(wait=True)
                return
            except KeyboardInterrupt:
                self.control.cancel()


def _safe_add(screen, row: int, column: int, text: str, width: int, role: str = "plain") -> None:
    draw_text(screen, row, column, text, width, role)


def _task_state(task: TaskProgressSnapshot) -> str:
    if task.complete:
        return "完成"
    if task.failed:
        return "失败"
    if task.cancelled:
        return "已取消"
    if task.started:
        return "进行中"
    return "等待"


def _pause_state(job: ScanJob) -> str:
    if job.paused:
        return "paused"
    if job.pause_requested:
        return "requested"
    return ""


def _scan_hints(pause_state: str) -> str:
    action = "Space 继续" if pause_state else "Space 暂停"
    return f"{action} · Q 取消审阅 · Ctrl-C 中断；扫描期间不接受清理选择"


def _draw_scan(
    screen,
    title: str,
    scope: str,
    job: ScanJob,
    *,
    stopping: bool = False,
) -> None:
    if small_screen(screen):
        draw_small_screen(screen)
        return
    screen.erase()
    height, width = screen.getmaxyx()
    pause_state = _pause_state(job)
    state = (
        "正在停止" if stopping else
        "已暂停" if pause_state == "paused" else
        "暂停已请求" if pause_state == "requested" else "正在扫描"
    )
    _safe_add(screen, 0, 1, f"{title} · {state}", width, "title")
    _safe_add(screen, 1, 1, clip_cells(scope, width - 3, tail=True), width, "muted")
    footer = draw_footer(screen, _scan_hints(pause_state))
    elapsed = time.monotonic() - job.started
    snapshot = job.snapshot
    if snapshot is None:
        _safe_add(screen, 3, 1, f"已用 {elapsed:.1f} 秒；正在准备任务清单", width)
        screen.refresh()
        return
    note = "；正在等待当前操作结束" if pause_state == "requested" else ""
    _safe_add(
        screen, 3, 1,
        f"已用 {elapsed:.1f} 秒 · 任务 {snapshot.completed_tasks}/{snapshot.total_tasks} 完成"
        f" · 任务进度 {snapshot.percent}%{note}",
        width,
    )
    active = [task for task in snapshot.tasks if task.started and not task.terminal]
    if active:
        shown = "、".join(task.label for task in active[:3])
        more = f" 等 {len(active)} 项" if len(active) > 3 else ""
        _safe_add(screen, 4, 1, f"进行中：{shown}{more}", width, "muted")
    row = 6
    for task in snapshot.tasks[:max(0, footer - row - 1)]:
        _safe_add(
            screen, row, 1,
            f"[{_task_state(task)}] {task.label} · 已处理 {task.processed_items} 项",
            width,
        )
        row += 1
    screen.refresh()


def _run_scan_screen(screen, action, *, title: str, scope: str) -> ScanScreenOutcome:
    screen.keypad(True)
    init_styles()
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    screen.timeout(50)
    job = ScanJob(action)
    worker_started = False
    try:
        # 首帧先于工作线程：初始化失败时尚未扫描，可回退既有文本流程。
        _draw_scan(screen, title, scope, job)
        job.start()
        worker_started = True
        drawn = 0.0
        while not job.done.is_set():
            now = time.monotonic()
            if now - drawn >= 0.1:
                _draw_scan(screen, title, scope, job)
                drawn = now
            key = screen.getch()
            if key in {ord("q"), ord("Q")}:
                _draw_scan(screen, title, scope, job, stopping=True)
                return ScanScreenOutcome("cancelled")
            if key == ord(" ") and not small_screen(screen) and not job.done.is_set():
                # 恢复与完成同时发生时，完成状态优先（循环条件先判定 done）。
                if job.pause_requested:
                    job.resume()
                else:
                    job.request_pause()
        if job.error is not None:
            return ScanScreenOutcome("done", None, job.error)
        # 扫描转审阅边界清理过期按键；Q 退出优先，Space/Enter 不泄漏进审阅页。
        screen.timeout(0)
        while True:
            stale = screen.getch()
            if stale == -1:
                break
            if stale in {ord("q"), ord("Q")}:
                return ScanScreenOutcome("cancelled")
        return ScanScreenOutcome("done", job.result)
    except (curses.error, OSError) as exc:
        if not worker_started:
            raise
        raise ScanScreenFailure(f"扫描界面绘制失败：{exc}") from exc
    finally:
        try:
            job.close()
        finally:
            screen.timeout(-1)


def start_scan_screen(action, *, title: str, scope: str) -> ScanScreenOutcome:
    """打开统一扫描界面；终端初始化失败抛 TUIUnavailable（未开始扫描）。"""
    try:
        return curses.wrapper(
            lambda screen: _run_scan_screen(screen, action, title=title, scope=scope)
        )
    except ScanScreenFailure:
        raise
    except (curses.error, OSError) as exc:
        raise TUIUnavailable(f"无法启动扫描界面：{exc}") from exc
