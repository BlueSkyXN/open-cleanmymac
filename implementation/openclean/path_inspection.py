"""有界、只读的内容保护检查；不跟随链接或枚举云占位目录。"""
from __future__ import annotations

import os
import stat
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from .filesystem import filesystem_id_retry, lstat_retry, scandir_entries
from .macos import scan_symlink_anchor
from .models import FileFacts, normalize_path
from .predicates import Predicate, ProtectionGate


class InspectionError(OSError):
    pass


class PathInspection:
    def __init__(
        self,
        protection: Predicate,
        *,
        checkpoint: Callable[[], None] = lambda: None,
        max_entries: int = 200_000,
        timeout: float = 5.0,
    ) -> None:
        self.protection = protection
        self.checkpoint = checkpoint
        self.remaining = max_entries
        self.timeout = timeout
        self.deadline = time.monotonic() + timeout

    def tick(self, *, cooperate: bool = True) -> None:
        if cooperate:
            before = time.monotonic()
            self.checkpoint()
            self.deadline += time.monotonic() - before
        if self.remaining <= 0 or time.monotonic() >= self.deadline:
            raise InspectionError("内容保护检查超出项数或时间预算")
        self.remaining -= 1

    def probe(self, path: Path) -> FileFacts:
        self.tick()
        try:
            ignored = isinstance(self.protection, ProtectionGate) and self.protection.knowledge_base_ignores(path)
        except Exception as exc:
            raise InspectionError("内容保护规则检查失败") from exc
        if ignored:
            raise InspectionError("内容保护范围命中忽略或保护规则")
        facts = FileFacts(path, lstat_retry(path))
        try:
            ignored = self.protection.should_ignore(facts)
        except Exception as exc:
            raise InspectionError("内容保护规则检查失败") from exc
        if ignored or facts.is_probable_cloud_placeholder:
            raise InspectionError("内容保护范围包含忽略/保护对象或云占位文件")
        return facts

    def checked_path(self, path: Path) -> FileFacts:
        path = normalize_path(path)
        anchor = scan_symlink_anchor(path)
        current = anchor
        for part in ("", *path.relative_to(anchor).parts):
            if part:
                current /= part
            facts = self.probe(current)
            if stat.S_ISLNK(facts.stat.st_mode):
                raise InspectionError("内容保护范围包含符号链接组件")
            if current != path and not stat.S_ISDIR(facts.stat.st_mode):
                raise InspectionError("内容保护范围的祖先不再是目录")
        return facts

    def tree(self, root: Path) -> Iterator[FileFacts]:
        original = self.checked_path(root)
        if not stat.S_ISDIR(original.stat.st_mode):
            raise InspectionError("内容保护目标不再是目录")
        filesystem = filesystem_id_retry(root)
        pending = [original]
        while pending:
            expected = pending.pop()
            current = self.checked_path(expected.path)
            if current.identity != expected.identity or not stat.S_ISDIR(current.stat.st_mode):
                raise InspectionError("内容保护检查期间目录身份变化")
            if current.stat.st_dev != original.stat.st_dev or filesystem_id_retry(current.path) != filesystem:
                raise InspectionError("内容保护范围跨越文件系统")
            for entry in scandir_entries(current.path):
                facts = self.probe(Path(entry.path))
                yield facts
                if stat.S_ISDIR(facts.stat.st_mode):
                    pending.append(facts)
            if self.probe(current.path).identity != current.identity:
                raise InspectionError("内容保护检查期间目录身份变化")

    def read_metadata(self, path: Path, limit: int = 4096) -> bytes:
        facts = self.checked_path(path)
        if not stat.S_ISREG(facts.stat.st_mode) or facts.stat.st_size > limit:
            raise InspectionError("Git 定位元数据类型或大小无效")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            opened = FileFacts(path, os.fstat(descriptor))
            if opened.identity != facts.identity or opened.is_probable_cloud_placeholder:
                raise InspectionError("Git 定位元数据身份变化")
            value = os.read(descriptor, limit + 1)
            if len(value) > limit:
                raise InspectionError("Git 定位元数据过大")
            return value
        finally:
            os.close(descriptor)
