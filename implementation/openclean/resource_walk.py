"""应用资源和文件诊断共用的有界只读遍历，不跟随链接。"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .filesystem import filesystem_id_retry
from .macos import scan_symlink_anchor
from .models import FileFacts, ScanIssue, ScanResult, normalize_path
from .predicates import Predicate, ProtectionGate


@dataclass
class WalkBudget:
    remaining: int = 200_000
    exhausted: bool = False


class ResourceExcluded(OSError):
    """策略排除与操作系统拒绝访问必须分别报告。"""

    def __init__(self, path: Path, code: str, message: str):
        super().__init__(message)
        self.path = path
        self.code = code


def _excluded(facts: FileFacts, protection: Predicate) -> bool:
    return (
        isinstance(protection, ProtectionGate)
        and protection.knowledge_base_ignores(facts.path)
    ) or protection.should_ignore(facts)


@contextmanager
def open_directory(path: Path, protection: Predicate):
    """逐组件打开目录，避免检查后祖先被替换成链接。"""
    path = normalize_path(path)
    anchor = scan_symlink_anchor(path)
    descriptor = os.open(anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        current = anchor
        for part in path.relative_to(anchor).parts:
            current /= part
            if isinstance(
                protection, ProtectionGate
            ) and protection.knowledge_base_ignores(current):
                raise ResourceExcluded(
                    current, "protected_path_skipped", "目录命中忽略或保护规则"
                )
            facts = FileFacts(
                current, os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            )
            if _excluded(facts, protection):
                raise ResourceExcluded(
                    current, "protected_path_skipped", "目录命中忽略或保护规则"
                )
            if facts.is_dataless:
                raise ResourceExcluded(
                    current, "dataless_object_skipped", "云占位目录未枚举"
                )
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            opened = FileFacts(current, os.fstat(child))
            if opened.identity != facts.identity or opened.is_dataless:
                os.close(child)
                raise OSError("目录身份发生变化")
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def walk_resources(
    root: Path,
    protection: Predicate,
    result: ScanResult,
    task: str,
    *,
    budget: WalkBudget | None = None,
    checkpoint: Callable[[], None] = lambda: None,
    on_progress: Callable[[], None] = lambda: None,
    descend: Callable[[FileFacts], bool] = lambda facts: True,
) -> Iterator[tuple[FileFacts, int]]:
    """返回元数据和父目录 fd；fd 仅在下一次迭代前有效。"""
    budget = budget or WalkBudget()
    root = normalize_path(root)

    def issue(path: Path, code: str, message: str, blocking: bool = True) -> None:
        result.issues.append(ScanIssue(code, message, task, path, blocking))

    def visit(path: Path, fd: int, device: int, filesystem: int, depth: int):
        if depth > 64:
            issue(path, "scan_limit_reached", "资源遍历超过目录深度上限")
            return
        with os.scandir(fd) as entries:
            for entry in entries:
                checkpoint()
                if budget.remaining <= 0:
                    if not budget.exhausted:
                        issue(path, "scan_limit_reached", "资源遍历达到目录项上限")
                        budget.exhausted = True
                    return
                budget.remaining -= 1
                on_progress()
                child_path = path / entry.name
                try:
                    if isinstance(
                        protection, ProtectionGate
                    ) and protection.knowledge_base_ignores(child_path):
                        continue
                    facts = FileFacts(
                        child_path,
                        os.stat(entry.name, dir_fd=fd, follow_symlinks=False),
                    )
                    if _excluded(facts, protection) or stat.S_ISLNK(facts.stat.st_mode):
                        continue
                    if facts.is_probable_cloud_placeholder:
                        issue(
                            child_path,
                            "dataless_object_skipped",
                            "dataless/零块疑似云占位对象未读取，不能据此确认其云端状态",
                            False,
                        )
                        continue
                    if facts.stat.st_dev != device:
                        issue(
                            child_path,
                            "cross_filesystem_skipped",
                            "跨设备对象未读取",
                            False,
                        )
                        continue
                    yield facts, fd
                    if stat.S_ISDIR(facts.stat.st_mode) and descend(facts):
                        child_fd = os.open(
                            entry.name,
                            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=fd,
                        )
                        try:
                            opened = FileFacts(child_path, os.fstat(child_fd))
                            if opened.identity != facts.identity or opened.is_dataless:
                                issue(child_path, "path_changed", "目录身份发生变化")
                            elif filesystem_id_retry(child_path) != filesystem:
                                issue(
                                    child_path,
                                    "cross_filesystem_skipped",
                                    "跨文件系统目录未读取",
                                    False,
                                )
                            else:
                                yield from visit(
                                    child_path, child_fd, device, filesystem, depth + 1
                                )
                        finally:
                            os.close(child_fd)
                except OSError as exc:
                    issue(
                        child_path,
                        "permission_denied"
                        if isinstance(exc, PermissionError)
                        else "path_disappeared"
                        if isinstance(exc, FileNotFoundError)
                        else "filesystem_error",
                        str(exc),
                    )
        latest = FileFacts.from_path(path)
        if latest.identity != FileFacts(path, os.fstat(fd)).identity:
            issue(path, "path_changed", "目录在遍历期间被替换或移走")

    try:
        with open_directory(root, protection) as fd:
            yield from visit(
                root, fd, os.fstat(fd).st_dev, filesystem_id_retry(root), 0
            )
    except ResourceExcluded as exc:
        issue(exc.path, exc.code, str(exc), False)
    except OSError as exc:
        issue(
            root,
            "permission_denied"
            if isinstance(exc, PermissionError)
            else "filesystem_error",
            str(exc),
        )


@contextmanager
def open_resource(facts: FileFacts, parent_fd: int):
    """只读打开已枚举的普通文件，读取前后复核身份与修改状态。"""
    descriptor = os.open(
        facts.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd
    )
    try:
        before = os.fstat(descriptor)
        opened = FileFacts(facts.path, before)
        if (
            opened.identity != facts.identity
            or not stat.S_ISREG(before.st_mode)
            or opened.is_probable_cloud_placeholder
            or (before.st_size, before.st_mtime_ns)
            != (facts.stat.st_size, facts.stat.st_mtime_ns)
        ):
            raise OSError("资源身份或内容状态发生变化")
        yield descriptor
        after = os.fstat(descriptor)
        if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            raise OSError("资源在读取期间发生变化")
    finally:
        os.close(descriptor)
