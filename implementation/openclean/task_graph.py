"""小型依赖任务图执行器：拓扑校验、并发调度和失败隔离。"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from types import MappingProxyType
from typing import Generic, TypeVar

T = TypeVar("T")


class TaskGraphError(ValueError):
    pass


@dataclass(frozen=True)
class TaskSpec(Generic[T]):
    identifier: str
    action: Callable[[], T]
    dependencies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.identifier:
            raise TaskGraphError("任务 identifier 不能为空")
        if self.identifier in self.dependencies:
            raise TaskGraphError(f"任务不能依赖自身：{self.identifier}")
        if len(set(self.dependencies)) != len(self.dependencies):
            raise TaskGraphError(f"任务依赖不能重复：{self.identifier}")


@dataclass(frozen=True)
class TaskOutcome(Generic[T]):
    identifier: str
    value: T | None = None
    error: Exception | None = None
    blocked_by: tuple[str, ...] = ()

    @property
    def successful(self) -> bool:
        return self.error is None and not self.blocked_by

    @property
    def executed(self) -> bool:
        return not self.blocked_by


@dataclass(frozen=True)
class TaskGraphResult(Generic[T]):
    outcomes: tuple[TaskOutcome[T], ...]

    @property
    def by_identifier(self):
        return MappingProxyType(
            {outcome.identifier: outcome for outcome in self.outcomes}
        )


def _validate_graph(tasks: list[TaskSpec[T]]) -> None:
    identifiers = [task.identifier for task in tasks]
    if len(set(identifiers)) != len(identifiers):
        raise TaskGraphError("任务 identifier 必须唯一")
    known = set(identifiers)
    for task in tasks:
        unknown = sorted(set(task.dependencies) - known)
        if unknown:
            raise TaskGraphError(
                f"任务 {task.identifier} 包含未知依赖：{', '.join(unknown)}"
            )

    remaining = {
        task.identifier: set(task.dependencies)
        for task in tasks
    }
    resolved: set[str] = set()
    while remaining:
        ready = [
            identifier
            for identifier in identifiers
            if identifier in remaining
            and remaining[identifier] <= resolved
        ]
        if not ready:
            cycle = ", ".join(
                identifier
                for identifier in identifiers
                if identifier in remaining
            )
            raise TaskGraphError(f"任务依赖存在环：{cycle}")
        resolved.update(ready)
        for identifier in ready:
            del remaining[identifier]


def execute_task_graph(
    specs: Iterable[TaskSpec[T]],
    *,
    workers: int,
) -> TaskGraphResult[T]:
    """并发执行依赖已满足的任务；失败只阻断其下游依赖。"""
    if workers < 1:
        raise TaskGraphError("workers 必须大于 0")
    tasks = list(specs)
    _validate_graph(tasks)
    if not tasks:
        return TaskGraphResult(())

    order = {task.identifier: index for index, task in enumerate(tasks)}
    by_identifier = {task.identifier: task for task in tasks}
    pending = set(by_identifier)
    outcomes: dict[str, TaskOutcome[T]] = {}
    running: dict[Future[T], str] = {}

    with ThreadPoolExecutor(max_workers=workers) as executor:
        while pending or running:
            for task in tasks:
                identifier = task.identifier
                if identifier not in pending:
                    continue
                if not all(dependency in outcomes for dependency in task.dependencies):
                    continue
                failed_dependencies = tuple(
                    dependency
                    for dependency in task.dependencies
                    if not outcomes[dependency].successful
                )
                pending.remove(identifier)
                if failed_dependencies:
                    outcomes[identifier] = TaskOutcome(
                        identifier=identifier,
                        blocked_by=failed_dependencies,
                    )
                else:
                    running[executor.submit(task.action)] = identifier

            if not running:
                continue
            completed, _ = wait(running, return_when=FIRST_COMPLETED)
            for future in sorted(
                completed,
                key=lambda candidate: order[running[candidate]],
            ):
                identifier = running.pop(future)
                try:
                    value = future.result()
                except Exception as exc:  # noqa: BLE001 - outcome owns task failures
                    outcomes[identifier] = TaskOutcome(
                        identifier=identifier,
                        error=exc,
                    )
                else:
                    outcomes[identifier] = TaskOutcome(
                        identifier=identifier,
                        value=value,
                    )

    return TaskGraphResult(
        tuple(outcomes[task.identifier] for task in tasks)
    )
