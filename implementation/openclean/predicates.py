"""可组合的扫描忽略谓词与不可绕过的保护闸。"""
from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .knowledge_base import KnowledgeBase
from .models import FileFacts, normalize_path


@runtime_checkable
class Predicate(Protocol):
    def should_ignore(self, item: FileFacts) -> bool:
        """返回 true 表示该对象不应进入扫描结果。"""
        ...


@dataclass(frozen=True)
class AllPredicate:
    predicates: Sequence[Predicate]

    def should_ignore(self, item: FileFacts) -> bool:
        return all(predicate.should_ignore(item) for predicate in self.predicates)


@dataclass(frozen=True)
class AnyPredicate:
    predicates: Sequence[Predicate]

    def should_ignore(self, item: FileFacts) -> bool:
        return any(predicate.should_ignore(item) for predicate in self.predicates)


@dataclass(frozen=True)
class KnowledgeBaseIgnorePredicate:
    knowledge_base: KnowledgeBase

    def should_ignore(self, item: FileFacts) -> bool:
        # 按规格同时查询 URL 与 path；任一命中即停止。
        return self.knowledge_base.should_ignore_url(
            item.file_url
        ) or self.knowledge_base.should_ignore_path(item.path)


@dataclass(frozen=True)
class FileNamePredicate:
    patterns: Sequence[str]

    def should_ignore(self, item: FileFacts) -> bool:
        return any(fnmatch.fnmatchcase(item.name, pattern) for pattern in self.patterns)


@dataclass(frozen=True)
class PathPatternPredicate:
    globs: Sequence[str] = ()
    regexes: Sequence[str] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "_compiled", tuple(re.compile(value) for value in self.regexes))

    def should_ignore(self, item: FileFacts) -> bool:
        candidate = str(item.path)
        return any(fnmatch.fnmatchcase(candidate, pattern) for pattern in self.globs) or any(
            pattern.search(candidate) for pattern in self._compiled
        )


@dataclass(frozen=True)
class FileSizePredicate:
    """忽略落在允许大小范围之外的文件。"""

    min_bytes: int | None = None
    max_bytes: int | None = None

    def __post_init__(self) -> None:
        if self.min_bytes is None and self.max_bytes is None:
            raise ValueError("min_bytes 和 max_bytes 至少设置一个")
        if self.min_bytes is not None and self.min_bytes < 0:
            raise ValueError("min_bytes 不能为负数")
        if self.max_bytes is not None and self.max_bytes < 0:
            raise ValueError("max_bytes 不能为负数")
        if (
            self.min_bytes is not None
            and self.max_bytes is not None
            and self.min_bytes > self.max_bytes
        ):
            raise ValueError("min_bytes 不能大于 max_bytes")

    def should_ignore(self, item: FileFacts) -> bool:
        if item.stat is None:
            return True
        size = item.stat.st_size
        if self.min_bytes is not None and size < self.min_bytes:
            return True
        return self.max_bytes is not None and size > self.max_bytes


class MissingPathPredicate:
    def should_ignore(self, item: FileFacts) -> bool:
        return item.stat is None


@dataclass(frozen=True)
class SubstringPathPredicate:
    patterns: Sequence[str]

    def should_ignore(self, item: FileFacts) -> bool:
        candidate = str(item.path)
        return any(pattern in candidate for pattern in self.patterns)


class ProtectionGate:
    """保证知识库保护规则永远在普通忽略条件之前求值。"""

    def __init__(
        self,
        knowledge_base: KnowledgeBase | None = None,
        predicates: Sequence[Predicate] = (),
    ) -> None:
        self._knowledge_base = KnowledgeBaseIgnorePredicate(
            knowledge_base or KnowledgeBase.empty()
        )
        self._predicates = tuple(predicates)

    def should_ignore(self, item: FileFacts) -> bool:
        if self._knowledge_base.should_ignore(item):
            return True
        return any(predicate.should_ignore(item) for predicate in self._predicates)

    def knowledge_base_ignores(self, path: str | os.PathLike[str]) -> bool:
        """无需访问文件系统即可执行最外层知识库安全判定。"""
        facts = FileFacts(path=normalize_path(path), stat=None)
        return self._knowledge_base.should_ignore(facts)

    def ignored(self, path: str | os.PathLike[str]) -> bool:
        return self.should_ignore(FileFacts.from_path(path))
