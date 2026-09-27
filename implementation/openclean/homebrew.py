"""Homebrew 缓存活动与锁范围的只读保护。"""
from __future__ import annotations

import os
import shlex
import stat
from collections.abc import Callable
from pathlib import Path

from .models import normalize_path
from .path_inspection import PathInspection
from .predicates import Predicate
from .processes import ProcessSnapshot

HOMEBREW_PROCESS_MARKERS = ("brew", "brew.rb")


def cache_roots(home: Path) -> tuple[Path, ...]:
    roots = [home / "Library/Caches/Homebrew"]
    value = os.environ.get("HOMEBREW_CACHE")
    if value:
        path = normalize_path(value)
        if any(path.is_relative_to(base) for base in (home / "Library/Caches", home / ".cache")):
            roots.append(path)
    return tuple(dict.fromkeys(roots))


def lock_roots() -> tuple[Path, ...]:
    roots = [Path("/opt/homebrew/var/homebrew/locks"), Path("/usr/local/var/homebrew/locks")]
    prefix = os.environ.get("HOMEBREW_PREFIX")
    if prefix and Path(prefix).is_absolute():
        roots.append(normalize_path(prefix) / "var/homebrew/locks")
    return tuple(dict.fromkeys(roots))


def running(snapshot: ProcessSnapshot) -> bool:
    for command in snapshot.commands:
        try:
            arguments = shlex.split(command)
        except ValueError:
            arguments = command.split()
        if any(Path(argument).name in HOMEBREW_PROCESS_MARKERS for argument in arguments):
            return True
    return False


def is_activity_marker(name: str) -> bool:
    return name.endswith((".incomplete", ".lock")) or name == "locks"


def cache_block_reason(
    path: Path, protection: Predicate, checkpoint: Callable[[], None] = lambda: None,
) -> str:
    if is_activity_marker(path.name):
        return "Homebrew 范围包含在途下载或锁；保留原位，不按年龄清理"
    inspection = PathInspection(protection, checkpoint=checkpoint)
    facts = inspection.checked_path(path)
    if not stat.S_ISDIR(facts.stat.st_mode):
        return ""
    for child in inspection.tree(path):
        if is_activity_marker(child.path.name):
            return "Homebrew 范围包含在途下载或锁；保留原位，不按年龄清理"
    return ""
