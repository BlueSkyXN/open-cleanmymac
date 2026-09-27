"""Homebrew 缓存活动与锁范围的只读保护。"""
from __future__ import annotations

import os
import re
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


def _brew_command(arguments: list[str], depth: int = 0) -> bool:
    if not arguments or depth > 4:
        return False
    executable = Path(arguments[0]).name
    if executable in HOMEBREW_PROCESS_MARKERS:
        return True
    if executable == "env":
        index = 1
        while index < len(arguments):
            value = arguments[index]
            if value == "--":
                index += 1
                break
            if value in {"-u", "--unset", "-C", "--chdir"}:
                index += 2
            elif value in {"-i", "--ignore-environment", "-"} or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", value):
                index += 1
            else:
                break
        return _brew_command(arguments[index:], depth + 1)
    if executable == "exec":
        return _brew_command(arguments[1:], depth + 1)
    shell = executable in {"sh", "bash", "zsh", "dash", "ksh"}
    ruby = re.fullmatch(r"ruby(?:[0-9]+(?:\.[0-9]+)*)?", executable) is not None
    if not shell and not ruby:
        return False
    index = 1
    while index < len(arguments):
        value = arguments[index]
        if value == "--":
            index += 1
            break
        if not value.startswith(("-", "+")):
            break
        if shell:
            if value.startswith("-") and not value.startswith("--") and "c" in value[1:]:
                if index + 1 >= len(arguments):
                    return False
                try:
                    command = shlex.split(arguments[index + 1])
                except ValueError:
                    return False
                return _brew_command(command, depth + 1)
            if value.startswith("-") and not value.startswith("--") and "s" in value[1:]:
                return False
            takes_value = value in {"-o", "+o", "-O", "+O", "--rcfile", "--init-file"}
        else:
            # -e 的后续参数是程序参数，不是 Ruby 脚本；-c 只做语法检查。
            if value.startswith("-e") or value == "-c":
                return False
            takes_value = value in {"-I", "-r", "-C", "-E", "-F", "-K", "-W", "--encoding"}
        index += 2 if takes_value else 1
    return index < len(arguments) and Path(arguments[index]).name in HOMEBREW_PROCESS_MARKERS


def running(snapshot: ProcessSnapshot) -> bool:
    for command in snapshot.commands:
        try:
            arguments = shlex.split(command)
        except ValueError:
            arguments = command.split()
        if _brew_command(arguments):
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
