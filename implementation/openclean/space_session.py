"""Analyze 私有线程与会话缓存；不写 Run Store，不执行清理。"""
from __future__ import annotations

import stat
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .analyzer import AnalysisUpdate, SpaceAnalysis, analyze_path
from .macos import scan_symlink_anchor, symlink_component
from .scan_tui import PauseObservableControl

__all__ = [
    "AnalysisJob",
    "CachedView",
    "PauseObservableControl",
    "SpaceCache",
]


class AnalysisJob:
    def __init__(self, path, protection, analyzer=analyze_path):
        self.control = PauseObservableControl()
        self.pause_requested = False
        self.started = time.monotonic()
        self.done = threading.Event()
        self.result: SpaceAnalysis | None = None
        self.error: BaseException | None = None
        self._lock = threading.Lock()
        self._update: AnalysisUpdate | None = None

        def publish(update):
            with self._lock:
                self._update = update

        def run():
            try:
                options = {"protection": protection, "control": self.control}
                if analyzer is analyze_path:
                    options["on_update"] = publish
                    options["include_empty"] = True
                self.result = self.control.run_task(analyzer, path, **options)
            except BaseException as exc:
                self.error = exc
            finally:
                self.done.set()

        self._run = run
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="analyze-session")
        self._future = None

    def start(self):
        self._future = self._pool.submit(self._run)

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
    def update(self):
        with self._lock:
            return self._update

    def close(self):
        self.control.cancel()
        # 重复 Ctrl-C 也不能让仍在访问目录的 worker 脱离生命周期。
        while True:
            try:
                self._pool.shutdown(wait=False, cancel_futures=True)
                # Python 3.11 的 Thread.join 被 SIGINT 中断后可能提前返回；
                # 先等 worker 自己发出的完成信号，再回收线程。
                if self._future is not None and not self._future.cancelled():
                    self.done.wait()
                self._pool.shutdown(wait=True)
                return
            except KeyboardInterrupt:
                self.control.cancel()


def _identity(path: Path):
    if symlink_component(path, anchor=scan_symlink_anchor(path)):
        return None
    value = path.lstat()
    if not stat.S_ISDIR(value.st_mode):
        return None
    return value.st_dev, value.st_ino, value.st_uid, getattr(value, "st_flags", 0)


@dataclass
class CachedView:
    analysis: SpaceAnalysis
    identity: tuple
    cursor: int = 0
    observed_at: float = 0.0


class SpaceCache:
    def __init__(self, max_views=16, max_entries=20_000):
        self.max_views = max_views
        self.max_entries = max_entries
        self._views: OrderedDict[Path, CachedView] = OrderedDict()

    @staticmethod
    def _count(analysis):
        return len(analysis.browse_entries if analysis.browse_entries is not None else analysis.entries)

    def put(self, analysis, cursor=0):
        self._views.pop(analysis.root, None)
        if analysis.cancelled or self._count(analysis) > self.max_entries:
            return
        try:
            identity = _identity(analysis.root)
        except OSError:
            return
        if identity is None or identity != analysis.root_identity:
            return
        self._views[analysis.root] = CachedView(analysis, identity, cursor, time.monotonic())
        while (len(self._views) > self.max_views or
               sum(self._count(view.analysis) for view in self._views.values()) > self.max_entries):
            self._views.popitem(last=False)

    def get(self, path):
        view = self._views.get(path)
        if view is None:
            return None
        try:
            same = _identity(path) == view.identity
        except OSError:
            same = False
        if not same:
            self._views.pop(path, None)
            return None
        self._views.move_to_end(path)
        return view

    def remember_cursor(self, path, cursor):
        if path in self._views:
            self._views[path].cursor = cursor

    def discard(self, path):
        self._views.pop(path, None)
