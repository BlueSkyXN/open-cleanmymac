"""失效 launchd 启动项扫描与重新判定。"""
from __future__ import annotations

import os
import plistlib
import shutil
import stat
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from .macos import nonprivileged_action_block_reason
from .models import FileFacts, Item, ScanIssue, ScanResult, normalize_path
from .predicates import Predicate, ProtectionGate


class StartupItemError(ValueError):
    pass


class UnsupportedStartupItem(StartupItemError):
    pass


@dataclass(frozen=True)
class StartupProgram:
    value: str
    uses_standard_path: bool = False


def read_startup_program(path: str | os.PathLike[str]) -> StartupProgram:
    """读取 launchd 的实际程序引用，不执行 shell 展开。"""
    source = normalize_path(path)
    try:
        with source.open("rb") as stream:
            payload = plistlib.load(stream)
    except (OSError, plistlib.InvalidFileException) as exc:
        raise StartupItemError(f"无法读取 plist：{exc}") from exc
    if not isinstance(payload, dict):
        raise StartupItemError("plist 根对象不是字典")

    if "Program" in payload:
        program = payload["Program"]
        if not isinstance(program, str) or not program.strip():
            raise StartupItemError("Program 不是非空字符串")
        if not os.path.isabs(program):
            raise StartupItemError("Program 不是绝对路径")
        return StartupProgram(program)

    if "BundleProgram" in payload:
        raise UnsupportedStartupItem(
            "BundleProgram 需要 SMAppService app-bundle 上下文"
        )

    arguments = payload.get("ProgramArguments")
    if (
        not isinstance(arguments, list)
        or not arguments
        or not isinstance(arguments[0], str)
        or not arguments[0].strip()
    ):
        raise StartupItemError("缺少有效的 Program/ProgramArguments")
    program = arguments[0]
    if os.path.isabs(program):
        return StartupProgram(program)
    if "/" in program:
        raise UnsupportedStartupItem("无法可靠解析包含目录的相对程序路径")
    return StartupProgram(program, uses_standard_path=True)


def startup_program_exists(program: StartupProgram) -> bool:
    if program.uses_standard_path:
        try:
            search_path = os.confstr("CS_PATH")
        except (OSError, ValueError):
            search_path = "/usr/bin:/bin:/usr/sbin:/sbin"
        return shutil.which(program.value, path=search_path) is not None
    try:
        return stat.S_ISREG(os.stat(program.value).st_mode)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as exc:
        raise StartupItemError(
            f"无法检查程序路径 {program.value}：{exc}"
        ) from exc


def startup_item_still_broken(
    path: str | os.PathLike[str],
    expected_program: str,
    expected_uses_standard_path: bool,
) -> bool:
    current = read_startup_program(path)
    expected = StartupProgram(
        expected_program,
        uses_standard_path=expected_uses_standard_path,
    )
    if current != expected:
        raise StartupItemError("启动项引用的程序已变化")
    return not startup_program_exists(current)


def _knowledge_base_ignores(protection: Predicate, path: Path) -> bool:
    return isinstance(protection, ProtectionGate) and (
        protection.knowledge_base_ignores(path)
    )


def _filesystem_issue(
    error: OSError,
    path: Path,
    task: str,
) -> ScanIssue:
    if isinstance(error, PermissionError):
        code = "permission_denied"
    elif isinstance(error, FileNotFoundError):
        code = "path_disappeared"
    else:
        code = "filesystem_error"
    return ScanIssue(code=code, message=str(error), task=task, path=path)


def _informational_issue(
    code: str,
    message: str,
    path: Path,
    task: str,
) -> ScanIssue:
    return ScanIssue(
        code=code,
        message=message,
        task=task,
        path=path,
        blocking=False,
    )


def scan_broken_startup_items(
    roots: Iterable[str | os.PathLike[str]],
    protection: Predicate,
    *,
    category: str = "失效启动项",
    safety: str = "confirm",
    home: Path | None = None,
    checkpoint: Callable[[], None] | None = None,
    on_progress: Callable[[], None] | None = None,
) -> ScanResult:
    """枚举 launchd plist，仅报告其程序引用可确认不存在的项目。"""
    result = ScanResult()
    user_home = normalize_path(home or Path.home())
    check = checkpoint or (lambda: None)
    advance = on_progress or (lambda: None)

    for raw_root in roots:
        check()
        advance()
        root = normalize_path(raw_root)
        if _knowledge_base_ignores(protection, root):
            continue
        try:
            root_stat = root.lstat()
        except FileNotFoundError:
            continue
        except (PermissionError, OSError) as exc:
            result.issues.append(_filesystem_issue(exc, root, category))
            continue
        root_facts = FileFacts(path=root, stat=root_stat)
        if (
            protection.should_ignore(root_facts)
            or stat.S_ISLNK(root_stat.st_mode)
            or not stat.S_ISDIR(root_stat.st_mode)
        ):
            continue
        try:
            with os.scandir(root) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except (PermissionError, FileNotFoundError, OSError) as exc:
            result.issues.append(_filesystem_issue(exc, root, category))
            continue

        for entry in entries:
            check()
            advance()
            if Path(entry.name).suffix.casefold() != ".plist":
                continue
            path = normalize_path(entry.path)
            if _knowledge_base_ignores(protection, path):
                continue
            try:
                candidate_stat = path.lstat()
            except (PermissionError, FileNotFoundError, OSError) as exc:
                result.issues.append(_filesystem_issue(exc, path, category))
                continue
            facts = FileFacts(path=path, stat=candidate_stat)
            if (
                protection.should_ignore(facts)
                or stat.S_ISLNK(candidate_stat.st_mode)
                or not stat.S_ISREG(candidate_stat.st_mode)
            ):
                continue
            try:
                program = read_startup_program(path)
                exists = startup_program_exists(program)
            except UnsupportedStartupItem as exc:
                result.issues.append(
                    _informational_issue(
                        "startup_item_unverifiable",
                        str(exc),
                        path,
                        category,
                    )
                )
                continue
            except StartupItemError as exc:
                result.issues.append(
                    _informational_issue(
                        "startup_item_invalid",
                        str(exc),
                        path,
                        category,
                    )
                )
                continue
            if exists:
                continue
            try:
                verified_stat = path.lstat()
            except (PermissionError, FileNotFoundError, OSError) as exc:
                result.issues.append(_filesystem_issue(exc, path, category))
                continue
            if FileFacts(path=path, stat=verified_stat).identity != facts.identity:
                result.issues.append(
                    _informational_issue(
                        "startup_item_changed",
                        "plist 在扫描期间发生变化，已跳过",
                        path,
                        category,
                    )
                )
                continue

            is_cloud_file = facts.is_probable_cloud_placeholder
            allocated_size = 0 if is_cloud_file else facts.allocated_size
            requires_privilege = bool(
                nonprivileged_action_block_reason(path, home=user_home)
            )
            actionable = not is_cloud_file and not requires_privilege
            if is_cloud_file:
                block_reason = "云占位启动项不可自动清理"
                item_safety = "critical"
            elif requires_privilege:
                block_reason = "需要尚未实现的特权帮助器"
                item_safety = safety
            else:
                block_reason = ""
                item_safety = safety
            result.items.append(
                Item(
                    path=path,
                    size=allocated_size,
                    category=category,
                    safety=item_safety,
                    note=f"引用的程序不存在：{program.value}",
                    logical_size=facts.logical_size,
                    allocated_size=allocated_size,
                    is_cloud_file=is_cloud_file,
                    cloud_file_count=1 if is_cloud_file else 0,
                    cloud_logical_size=(
                        facts.logical_size if is_cloud_file else 0
                    ),
                    actionable=actionable,
                    action_block_reason=block_reason,
                    requires_privilege=requires_privilege,
                    identity=facts.identity,
                    preselected=False,
                    startup_program=program.value,
                    startup_program_uses_path=program.uses_standard_path,
                )
            )
    result.items.sort(key=lambda item: str(item.path))
    return result
