"""线程安全的加权进度模型与 TTY 单行渲染器。"""
from __future__ import annotations

import math
import sys
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TextIO


@dataclass(frozen=True)
class ProgressTaskSpec:
    identifier: str
    label: str
    weight: float = 1.0

    def __post_init__(self) -> None:
        if not self.identifier:
            raise ValueError("进度任务 identifier 不能为空")
        if self.weight <= 0 or not math.isfinite(self.weight):
            raise ValueError("进度任务 weight 必须是有限正数")


@dataclass(frozen=True)
class TaskProgressSnapshot:
    identifier: str
    label: str
    weight: float
    fraction: float
    processed_items: int
    complete: bool


@dataclass(frozen=True)
class ProgressSnapshot:
    sequence: int
    fraction: float
    completed_tasks: int
    total_tasks: int
    processed_items: int
    cancelled: bool
    tasks: tuple[TaskProgressSnapshot, ...]

    @property
    def percent(self) -> int:
        return min(100, max(0, round(self.fraction * 100)))

    @property
    def active_label(self) -> str:
        active = next(
            (task.label for task in self.tasks if not task.complete), ""
        )
        return active


@dataclass
class _TaskState:
    spec: ProgressTaskSpec
    fraction: float = 0.0
    processed_items: int = 0
    complete: bool = False


class TaskProgress:
    """单个任务的单调更新句柄。"""

    def __init__(self, owner: WeightedProgress, identifier: str) -> None:
        self._owner = owner
        self.identifier = identifier

    def advance(self, count: int = 1) -> None:
        if count < 0:
            raise ValueError("进度增量不能为负数")
        self._owner._advance(self.identifier, count)

    def set_fraction(self, fraction: float) -> None:
        self._owner._set_fraction(self.identifier, fraction)

    def complete(self) -> None:
        self._owner._complete(self.identifier)


class WeightedProgress:
    """固定任务集合的加权平均进度；所有公开快照均不可变。"""

    def __init__(
        self,
        specs: Iterable[ProgressTaskSpec],
        *,
        callback: Callable[[ProgressSnapshot], None] | None = None,
        smoothing_items: int = 64,
    ) -> None:
        spec_list = list(specs)
        identifiers = [spec.identifier for spec in spec_list]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("进度任务 identifier 必须唯一")
        if smoothing_items < 1:
            raise ValueError("smoothing_items 必须大于 0")
        self._states = {
            spec.identifier: _TaskState(spec=spec) for spec in spec_list
        }
        self._callback = callback
        self._smoothing_items = smoothing_items
        self._cancelled = False
        self._lock = threading.Lock()
        self._emit_lock = threading.RLock()
        self._last_emitted_sequence = -1
        self._sequence = 0

    def task(self, identifier: str) -> TaskProgress:
        if identifier not in self._states:
            raise KeyError(f"未知进度任务：{identifier}")
        return TaskProgress(self, identifier)

    def start(self) -> None:
        self._emit(self.snapshot())

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            self._sequence += 1
            snapshot = self._snapshot_locked()
        self._emit(snapshot)

    def snapshot(self) -> ProgressSnapshot:
        with self._lock:
            return self._snapshot_locked()

    def _advance(self, identifier: str, count: int) -> None:
        with self._lock:
            state = self._states[identifier]
            if state.complete:
                return
            state.processed_items += count
            heuristic = min(
                0.95,
                state.processed_items
                / (state.processed_items + self._smoothing_items),
            )
            state.fraction = max(state.fraction, heuristic)
            self._sequence += 1
            snapshot = self._snapshot_locked()
        self._emit(snapshot)

    def _set_fraction(self, identifier: str, fraction: float) -> None:
        if not math.isfinite(fraction) or not 0.0 <= fraction <= 1.0:
            raise ValueError("fraction 必须位于 0..1")
        with self._lock:
            state = self._states[identifier]
            if state.complete:
                return
            state.fraction = max(state.fraction, fraction)
            self._sequence += 1
            snapshot = self._snapshot_locked()
        self._emit(snapshot)

    def _complete(self, identifier: str) -> None:
        with self._lock:
            state = self._states[identifier]
            if state.complete:
                return
            state.fraction = 1.0
            state.complete = True
            self._sequence += 1
            snapshot = self._snapshot_locked()
        self._emit(snapshot)

    def _snapshot_locked(self) -> ProgressSnapshot:
        tasks = tuple(
            TaskProgressSnapshot(
                identifier=state.spec.identifier,
                label=state.spec.label,
                weight=state.spec.weight,
                fraction=state.fraction,
                processed_items=state.processed_items,
                complete=state.complete,
            )
            for state in self._states.values()
        )
        total_weight = sum(task.weight for task in tasks)
        fraction = (
            sum(task.fraction * task.weight for task in tasks) / total_weight
            if total_weight
            else 1.0
        )
        return ProgressSnapshot(
            sequence=self._sequence,
            fraction=fraction,
            completed_tasks=sum(task.complete for task in tasks),
            total_tasks=len(tasks),
            processed_items=sum(task.processed_items for task in tasks),
            cancelled=self._cancelled,
            tasks=tasks,
        )

    def _emit(self, snapshot: ProgressSnapshot) -> None:
        if self._callback is None:
            return
        with self._emit_lock:
            if snapshot.sequence <= self._last_emitted_sequence:
                return
            self._last_emitted_sequence = snapshot.sequence
            try:
                self._callback(snapshot)
            except Exception:  # noqa: BLE001 - UI observers cannot fail scan tasks
                return


class TerminalProgressRenderer:
    """节流的单行 TTY 进度；调用 ``finish`` 后恢复普通换行输出。"""

    def __init__(
        self,
        *,
        stream: TextIO | None = None,
        min_interval: float = 0.05,
        bar_width: int = 24,
    ) -> None:
        self.stream = stream or sys.stderr
        self.min_interval = min_interval
        self.bar_width = bar_width
        self._last_render = 0.0
        self._last_length = 0
        self._finished = False
        self._lock = threading.Lock()

    def __call__(self, snapshot: ProgressSnapshot) -> None:
        now = time.monotonic()
        with self._lock:
            if self._finished:
                return
            if (
                snapshot.fraction < 1.0
                and now - self._last_render < self.min_interval
            ):
                return
            self._last_render = now
            filled = min(
                self.bar_width,
                max(0, round(snapshot.fraction * self.bar_width)),
            )
            bar = "█" * filled + "░" * (self.bar_width - filled)
            label = snapshot.active_label or "完成"
            line = (
                f"扫描 [{bar}] {snapshot.percent:>3}% · "
                f"{snapshot.completed_tasks}/{snapshot.total_tasks} · {label}"
            )
            padding = " " * max(0, self._last_length - len(line))
            self.stream.write(f"\r{line}{padding}")
            self.stream.flush()
            self._last_length = len(line)

    def finish(self) -> None:
        with self._lock:
            if self._finished:
                return
            if self._last_length:
                self.stream.write("\n")
                self.stream.flush()
            self._finished = True
