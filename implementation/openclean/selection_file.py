"""经典清理的有界精确选择清单；清单只提供 selector，资格来自本次扫描。"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

MAX_SELECTION_BYTES = 8 * 1024 * 1024
MAX_SELECTORS = 50000
MAX_SELECTOR_LENGTH = 4096


class SelectionFileError(ValueError):
    pass


def _stamp(value: os.stat_result) -> tuple[int, ...]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def load_selection_file(path: Path) -> tuple[str, ...]:
    """读取一次非空 JSON 字符串数组，不接受扫描报告、通配符展开或默认选择。"""
    try:
        descriptor = os.open(path.expanduser(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise SelectionFileError("选择清单必须是普通文件")
            if before.st_size > MAX_SELECTION_BYTES:
                raise SelectionFileError("选择清单超过 8 MiB 上限")
            raw = stream.read(MAX_SELECTION_BYTES + 1)
            if len(raw) > MAX_SELECTION_BYTES or _stamp(os.fstat(stream.fileno())) != _stamp(before):
                raise SelectionFileError("选择清单在读取期间变化或超过大小上限")
        values = json.loads(raw.decode("utf-8"))
    except SelectionFileError:
        raise
    except OSError:
        raise SelectionFileError("无法读取选择清单；请使用可读取的普通文件，文件本身不能是符号链接") from None
    except (ValueError, UnicodeError, RecursionError):
        raise SelectionFileError("选择清单须为 UTF-8 JSON 字符串数组") from None
    if not isinstance(values, list) or not values:
        raise SelectionFileError("选择清单须为非空 JSON 字符串数组")
    if len(values) > MAX_SELECTORS:
        raise SelectionFileError(f"选择清单超过 {MAX_SELECTORS} 项上限")
    for index, value in enumerate(values, 1):
        if (not isinstance(value, str) or not value.strip() or "\x00" in value
                or len(value) > MAX_SELECTOR_LENGTH):
            raise SelectionFileError(f"选择清单第 {index} 项须为非空路径或资源 identifier，最长 {MAX_SELECTOR_LENGTH} 字符")
    return tuple(dict.fromkeys(values))
