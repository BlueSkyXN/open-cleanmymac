"""文件系统只读辅助函数。"""
from __future__ import annotations

import errno
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")


def retry_eintr(operation: Callable[[], T]) -> T:
    """仅在系统调用被信号中断时重试，其它错误保持原样。"""
    while True:
        try:
            return operation()
        except OSError as exc:
            if exc.errno != errno.EINTR:
                raise


def lstat_retry(path: Path) -> os.stat_result:
    return retry_eintr(path.lstat)


def filesystem_id_retry(path: Path) -> int:
    """返回路径所在文件系统 ID，并处理信号中断。"""
    return retry_eintr(lambda: os.statvfs(path)).f_fsid


def scandir_entries(path: str | os.PathLike[str]) -> Iterator[os.DirEntry[str]]:
    """迭代目录项，并对打开目录或读取下一项时的 EINTR 做透明重试。"""
    iterator = retry_eintr(lambda: os.scandir(path))
    with iterator:
        while True:
            try:
                yield retry_eintr(lambda: next(iterator))
            except StopIteration:
                return
