"""清理选择、安全预检与用户态执行。

普通文件系统候选仅移动到同一文件系统的 Trash；只有已经位于 Trash 根目录中的
内容才会永久删除。Docker 仅执行代码内审计过的 prune 白名单；特权路径与
Docker Local Volumes 保持不可执行。
"""
from __future__ import annotations

import ctypes
import errno
import os
import stat
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path

from .docker import (
    DockerPruneError,
    DockerTargetError,
    docker_prune_supported,
    parse_docker_resource_binding,
    prune_docker_resource,
)
from .macos import (
    discover_darwin_user_cache,
    nonprivileged_action_block_reason,
    symlink_component,
)
from .models import FileFacts, FileIdentity, Item, ScanResult, normalize_path
from .predicates import Predicate, ProtectionGate
from .processes import (
    ProcessDetectionError,
    ProcessSnapshot,
    capture_process_snapshot,
)
from .startup_items import StartupItemError, startup_item_still_broken
from .updater import assess_updater_candidate


class SelectionError(ValueError):
    pass


class CleanupSafetyError(RuntimeError):
    pass


_SUCCESS_STATUSES = frozenset({"moved_to_trash", "deleted", "pruned"})
_RENAME_EXCL = 0x00000004
_RENAME_NOFOLLOW_ANY = 0x00000010


def _load_renameatx_np():
    if sys.platform != "darwin":
        return None, None
    try:
        library = ctypes.CDLL(None, use_errno=True)
        function = library.renameatx_np
    except (AttributeError, OSError):
        return None, None
    function.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    function.restype = ctypes.c_int
    return library, function


_RENAME_LIBRARY, _RENAMEATX_NP = _load_renameatx_np()


def _item_key(item: Item) -> tuple[str, str, str, str]:
    location = str(item.path) if item.path is not None else item.identifier
    return item.resource_kind, location, item.domain, item.category


def _matches_selector(item: Item, selector: str) -> bool:
    if selector == item.identifier and item.identifier:
        return True
    if item.path is None:
        return False
    if selector == str(item.path):
        return True
    try:
        return normalize_path(selector) == item.path
    except (TypeError, ValueError, OSError):
        return False


def select_cleanup_items(
    items: Iterable[Item],
    *,
    selectors: Iterable[str] = (),
    select_all_safe: bool = False,
    include_confirm: bool = False,
    include_critical: bool = False,
) -> list[Item]:
    """根据默认选择或精确 selector 与显式授权生成执行集合。"""
    candidates = list(items)
    selector_values = tuple(selectors)
    if selector_values and select_all_safe:
        raise SelectionError("--select 不能与 --all 同时使用")

    selected_keys: set[tuple[str, str, str, str]] = set()
    if not selector_values:
        selected_keys.update(
            _item_key(item)
            for item in candidates
            if item.preselected is True
            and item.actionable
            and not item.requires_explicit_selection
        )
        if select_all_safe:
            selected_keys.update(
                _item_key(item)
                for item in candidates
                if item.safety == "safe"
                and item.actionable
                and not item.requires_explicit_selection
            )
        if include_confirm:
            selected_keys.update(
                _item_key(item)
                for item in candidates
                if item.safety == "confirm"
                and item.actionable
                and not item.requires_explicit_selection
            )
        if include_critical:
            selected_keys.update(
                _item_key(item)
                for item in candidates
                if item.safety == "critical"
                and item.actionable
                and not item.requires_explicit_selection
            )

    for selector in selector_values:
        matches = [
            item for item in candidates if _matches_selector(item, selector)
        ]
        if not matches:
            raise SelectionError(f"未找到清理候选：{selector}")
        if len(matches) > 1:
            raise SelectionError(f"清理候选不唯一：{selector}")
        item = matches[0]
        if not item.actionable:
            reason = item.action_block_reason or "该候选不可执行"
            raise SelectionError(f"拒绝选择 {selector}：{reason}")
        if item.safety == "confirm" and not include_confirm:
            raise SelectionError(
                f"{selector} 属于 confirm，需同时指定 --include-confirm"
            )
        if item.safety == "critical" and not include_critical:
            raise SelectionError(
                f"{selector} 属于 critical，需同时指定 --include-critical"
            )
        selected_keys.add(_item_key(item))

    return [item for item in candidates if _item_key(item) in selected_keys]


def with_cleanup_selection(
    result: ScanResult, selected: Iterable[Item]
) -> ScanResult:
    selected_keys = {_item_key(item) for item in selected}
    return ScanResult(
        items=[
            replace(item, preselected=_item_key(item) in selected_keys)
            for item in result.items
        ],
        issues=list(result.issues),
        cancelled=result.cancelled,
    )


@dataclass(frozen=True)
class CleanupOutcome:
    item: Item
    status: str
    bytes_affected: int = 0
    destination: Path | None = None
    message: str = ""

    @property
    def successful(self) -> bool:
        return self.status in _SUCCESS_STATUSES


@dataclass
class CleanupReport:
    outcomes: list[CleanupOutcome] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return all(outcome.successful for outcome in self.outcomes)

    @property
    def selected_count(self) -> int:
        return len(self.outcomes)

    @property
    def selected_bytes(self) -> int:
        return sum(outcome.item.size for outcome in self.outcomes)

    @property
    def moved_bytes(self) -> int:
        return sum(
            outcome.bytes_affected
            for outcome in self.outcomes
            if outcome.status == "moved_to_trash"
        )

    @property
    def deleted_bytes(self) -> int:
        return sum(
            outcome.bytes_affected
            for outcome in self.outcomes
            if outcome.status in {"deleted", "pruned"}
        )


def _same_or_descendant(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((str(path), str(root))) == str(root)
    except ValueError:
        return False


def _knowledge_base_blocks(protection: Predicate, path: Path) -> bool:
    if not isinstance(protection, ProtectionGate):
        return False
    return _evaluate_protection(
        lambda: protection.knowledge_base_ignores(path), path
    )


def _evaluate_protection(
    operation: Callable[[], bool], path: Path
) -> bool:
    try:
        return operation()
    except Exception as exc:
        raise CleanupSafetyError(f"保护规则判定失败 {path}：{exc}") from exc


def _predicate_blocks(protection: Predicate, facts: FileFacts) -> bool:
    return _evaluate_protection(
        lambda: protection.should_ignore(facts), facts.path
    )


def _lstat(path: Path) -> os.stat_result:
    try:
        return path.lstat()
    except FileNotFoundError as exc:
        raise CleanupSafetyError(f"路径已不存在：{path}") from exc
    except (PermissionError, OSError) as exc:
        raise CleanupSafetyError(f"无法检查路径 {path}：{exc}") from exc


def _validate_cleanup_scope(
    item: Item,
    path: Path,
    uid: int,
    home: Path,
) -> None:
    if not item.cleanup_scope:
        if reason := nonprivileged_action_block_reason(path, home=home):
            raise CleanupSafetyError(f"拒绝操作{reason}")
        anchor = home if _same_or_descendant(path, home) else Path("/")
        if component := symlink_component(path, anchor=anchor):
            raise CleanupSafetyError(
                f"候选路径包含符号链接组件：{component}"
            )
        return
    if item.cleanup_scope != "darwin-user-cache":
        raise CleanupSafetyError(f"未知特殊清理范围：{item.cleanup_scope}")
    assert item.cleanup_root is not None
    assert item.cleanup_root_identity is not None
    root = normalize_path(item.cleanup_root)
    if component := symlink_component(path, anchor=root):
        raise CleanupSafetyError(
            f"Darwin cache 候选包含符号链接组件：{component}"
        )
    if path.parent != root:
        raise CleanupSafetyError("Darwin cache 候选不是发现根的一级子项")
    discovery = discover_darwin_user_cache()
    if discovery.issues:
        raise CleanupSafetyError(
            f"无法重新发现 Darwin cache：{discovery.issues[0].message}"
        )
    if root not in discovery.paths:
        raise CleanupSafetyError("Darwin cache 根路径已变化")
    root_stat = _lstat(root)
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise CleanupSafetyError("Darwin cache 根路径不是可信目录")
    if FileFacts(path=root, stat=root_stat).identity != item.cleanup_root_identity:
        raise CleanupSafetyError("Darwin cache 根目录 inode 已变化")
    if root_stat.st_uid != uid:
        raise CleanupSafetyError("Darwin cache 根目录不属于当前用户")


def _validate_startup_item(item: Item, path: Path) -> None:
    if not item.startup_program:
        return

    def validate_source() -> None:
        live_stat = _lstat(path)
        if stat.S_ISLNK(live_stat.st_mode) or not stat.S_ISREG(
            live_stat.st_mode
        ):
            raise CleanupSafetyError(
                f"失效启动项已不再是普通文件：{path}"
            )
        live_facts = FileFacts(path=path, stat=live_stat)
        if live_facts.is_probable_cloud_placeholder:
            raise CleanupSafetyError(
                "失效启动项变成了 macOS dataless/疑似云占位文件："
                f"{path}"
            )
        if item.identity is not None and live_facts.identity != item.identity:
            raise CleanupSafetyError(f"失效启动项 inode 已变化：{path}")

    validate_source()
    try:
        still_broken = startup_item_still_broken(
            path,
            item.startup_program,
            item.startup_program_uses_path,
        )
    except StartupItemError as exc:
        raise CleanupSafetyError(f"无法重新验证失效启动项：{exc}") from exc
    validate_source()
    if not still_broken:
        raise CleanupSafetyError("启动项引用的程序已恢复，拒绝清理")


def _is_trash_root(path: Path, uid: int) -> bool:
    return path.name == ".Trash" or (
        path.name == str(uid) and path.parent.name == ".Trashes"
    )


@dataclass(frozen=True)
class _AuditedEntry:
    relative_parts: tuple[str, ...]
    identity: FileIdentity
    is_directory: bool


def _audit_descendants(
    root: Path,
    root_device: int,
    protection: Predicate,
    expected_uid: int,
    *,
    expected_root_identity: FileIdentity | None = None,
) -> tuple[_AuditedEntry, ...]:
    def validate_directory(directory: Path, stat_result: os.stat_result) -> None:
        if stat.S_ISLNK(stat_result.st_mode) or not stat.S_ISDIR(
            stat_result.st_mode
        ):
            raise CleanupSafetyError(
                f"候选后代已不再是普通目录：{directory}"
            )
        if stat_result.st_uid != expected_uid:
            raise CleanupSafetyError(
                f"候选后代目录不属于当前用户：{directory}"
            )
        if stat_result.st_dev != root_device:
            raise CleanupSafetyError(
                f"候选后代目录位于其他文件系统：{directory}"
            )
        facts = FileFacts(path=directory, stat=stat_result)
        if facts.is_probable_cloud_placeholder:
            raise CleanupSafetyError(
                f"发现 macOS dataless/疑似云占位目录：{directory}"
            )

    audited: list[_AuditedEntry] = []
    stack = [(root, ())]
    while stack:
        directory, directory_parts = stack.pop()
        directory_stat = _lstat(directory)
        validate_directory(directory, directory_stat)
        directory_fd = _open_directory_no_follow(directory, anchor=root)
        try:
            live_stat = os.fstat(directory_fd)
            validate_directory(directory, live_stat)
            if (
                not directory_parts
                and expected_root_identity is not None
                and FileIdentity.from_stat(live_stat) != expected_root_identity
            ):
                raise CleanupSafetyError(
                    f"候选根目录 inode 已变化：{directory}"
                )
            with os.scandir(directory_fd) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except CleanupSafetyError:
            raise
        except (PermissionError, FileNotFoundError, OSError) as exc:
            raise CleanupSafetyError(
                f"无法复核目录 {directory}：{exc}"
            ) from exc
        finally:
            os.close(directory_fd)
        for entry in entries:
            path = normalize_path(directory / entry.name)
            relative_parts = (*directory_parts, entry.name)
            if _knowledge_base_blocks(protection, path):
                raise CleanupSafetyError(f"命中忽略或保护规则：{path}")
            stat_result = _lstat(path)
            if stat_result.st_uid != expected_uid:
                raise CleanupSafetyError(
                    f"候选后代不属于当前用户：{path}"
                )
            facts = FileFacts(path=path, stat=stat_result)
            if _predicate_blocks(protection, facts):
                raise CleanupSafetyError(f"命中忽略或保护规则：{path}")
            if facts.is_probable_cloud_placeholder:
                raise CleanupSafetyError(
                    f"发现 macOS dataless/疑似云占位文件：{path}"
                )
            audited.append(
                _AuditedEntry(
                    relative_parts=relative_parts,
                    identity=FileIdentity.from_stat(stat_result),
                    is_directory=stat.S_ISDIR(stat_result.st_mode),
                )
            )
            if stat.S_ISLNK(stat_result.st_mode):
                continue
            if stat.S_ISDIR(stat_result.st_mode):
                if stat_result.st_dev != root_device:
                    raise CleanupSafetyError(f"目录包含其他文件系统：{path}")
                stack.append((path, relative_parts))
    return tuple(
        sorted(
            audited,
            key=lambda entry: (-len(entry.relative_parts), entry.relative_parts),
        )
    )


def _audit_item(
    item: Item,
    protection: Predicate,
    home: Path,
    uid: int,
    process_snapshot: ProcessSnapshot | None,
    process_error: str,
) -> os.stat_result | None:
    if not item.actionable:
        raise CleanupSafetyError(
            item.action_block_reason or "扫描结果已标记为不可执行"
        )
    if item.resource_kind == "docker":
        if not docker_prune_supported(item.identifier):
            raise CleanupSafetyError(
                f"不支持自动清理 Docker 资源：{item.identifier}"
            )
        try:
            parse_docker_resource_binding(item.resource_binding)
        except DockerTargetError as exc:
            raise CleanupSafetyError(str(exc)) from exc
        return None
    if item.running_process_markers:
        if process_error:
            raise CleanupSafetyError(process_error)
        if process_snapshot is None:
            raise CleanupSafetyError("缺少运行中进程快照")
        if process_snapshot.any_running(item.running_process_markers):
            raise CleanupSafetyError(
                "相关工具已启动或正在运行，拒绝清理其缓存"
            )
    if item.resource_kind != "filesystem" or item.path is None:
        raise CleanupSafetyError("当前执行器不支持该资源类型")
    if item.identity is None:
        raise CleanupSafetyError("候选缺少扫描时 inode 身份，拒绝执行")
    if item.requires_privilege:
        raise CleanupSafetyError("该候选需要尚未实现的特权帮助器")
    if item.excluded_paths:
        raise CleanupSafetyError("候选包含忽略或保护路径")
    if item.cloud_file_count or item.is_cloud_file:
        raise CleanupSafetyError("候选包含 macOS dataless/疑似云占位文件")

    path = normalize_path(item.path)
    if item.updater_status:
        current_updater = assess_updater_candidate(path, home=home)
        if current_updater is None:
            raise CleanupSafetyError(
                "updater 暂存状态已变化，需重新扫描后再决定"
            )
        if (
            current_updater.status != item.updater_status
            or current_updater.installed_version != item.installed_version
            or current_updater.staged_version != item.staged_version
        ):
            raise CleanupSafetyError(
                "updater 版本状态已变化，需重新扫描后再决定"
            )
        if current_updater.blocks_cleanup:
            raise CleanupSafetyError(current_updater.block_reason)
    _validate_cleanup_scope(item, path, uid, home)
    if item.domain == "trash" and not _is_trash_root(path, uid):
        raise CleanupSafetyError(f"拒绝清空非 Trash 根目录：{path}")
    if _knowledge_base_blocks(protection, path):
        raise CleanupSafetyError(f"命中忽略或保护规则：{path}")

    stat_result = _lstat(path)
    if stat_result.st_uid != uid:
        raise CleanupSafetyError(f"候选不属于当前用户：{path}")
    if stat.S_ISLNK(stat_result.st_mode):
        raise CleanupSafetyError(f"候选根路径变成了符号链接：{path}")
    facts = FileFacts(path=path, stat=stat_result)
    if _predicate_blocks(protection, facts):
        raise CleanupSafetyError(f"命中忽略或保护规则：{path}")
    if facts.is_probable_cloud_placeholder:
        raise CleanupSafetyError(
            f"候选变成了 macOS dataless/疑似云占位文件：{path}"
        )
    if item.identity is not None and facts.identity != item.identity:
        raise CleanupSafetyError(f"候选 inode 已变化：{path}")
    _validate_startup_item(item, path)
    if stat.S_ISDIR(stat_result.st_mode):
        _audit_descendants(path, stat_result.st_dev, protection, uid)
    return stat_result


def trash_directory_for(
    path: str | os.PathLike[str],
    *,
    home: Path | None = None,
    uid: int | None = None,
) -> Path:
    """返回与候选同卷的用户 Trash，并在安全范围内创建用户目录。"""
    candidate = normalize_path(path)
    home = normalize_path(home or Path.home())
    uid = os.getuid() if uid is None else uid
    candidate_stat = _lstat(candidate)
    home_stat = _lstat(home)

    if candidate_stat.st_dev == home_stat.st_dev:
        trash = home / ".Trash"
        trash_parent = home
        parent_anchor = home
        parent_identity = FileIdentity.from_stat(home_stat)
    else:
        mount_root = candidate if stat.S_ISDIR(candidate_stat.st_mode) else candidate.parent
        while mount_root.parent != mount_root:
            parent_stat = _lstat(mount_root.parent)
            if parent_stat.st_dev != candidate_stat.st_dev:
                break
            mount_root = mount_root.parent
        trashes_root = mount_root / ".Trashes"
        trashes_stat = _lstat(trashes_root)
        if stat.S_ISLNK(trashes_stat.st_mode) or not stat.S_ISDIR(
            trashes_stat.st_mode
        ):
            raise CleanupSafetyError(f"外置卷 Trash 根目录无效：{trashes_root}")
        trash = trashes_root / str(uid)
        trash_parent = trashes_root
        parent_anchor = mount_root
        parent_identity = FileIdentity.from_stat(trashes_stat)

    _prepare_trash_directory(
        trash,
        parent=trash_parent,
        parent_anchor=parent_anchor,
        expected_parent_identity=parent_identity,
        expected_device=candidate_stat.st_dev,
        uid=uid,
    )
    return trash


def _unique_destination(trash: Path, source: Path) -> Path:
    destination = trash / source.name
    suffix = 2
    while os.path.lexists(destination):
        destination = trash / f"{source.name} {suffix}"
        suffix += 1
    return destination


def _identity_matches(path: Path, item: Item) -> bool:
    if item.identity is None:
        return True
    return FileFacts(path=path, stat=_lstat(path)).identity == item.identity


def _validate_trash_directory_stat(
    path: Path,
    stat_result: os.stat_result,
    *,
    expected_device: int,
    uid: int,
    expected_identity: FileIdentity | None = None,
) -> None:
    if stat.S_ISLNK(stat_result.st_mode) or not stat.S_ISDIR(
        stat_result.st_mode
    ):
        raise CleanupSafetyError(f"Trash 路径无效：{path}")
    if stat_result.st_dev != expected_device:
        raise CleanupSafetyError(f"Trash 与候选不在同一文件系统：{path}")
    if stat_result.st_uid != uid:
        raise CleanupSafetyError(f"Trash 目录不属于当前用户：{path}")
    if stat.S_IMODE(stat_result.st_mode) & 0o077:
        raise CleanupSafetyError(f"Trash 目录必须使用私有权限：{path}")
    if (
        expected_identity is not None
        and FileIdentity.from_stat(stat_result) != expected_identity
    ):
        raise CleanupSafetyError(f"Trash 目录 inode 已变化：{path}")


def _prepare_trash_directory(
    trash: Path,
    *,
    parent: Path,
    parent_anchor: Path,
    expected_parent_identity: FileIdentity,
    expected_device: int,
    uid: int,
) -> None:
    """在可信父目录 fd 下创建或打开 per-user Trash，并绑定最终身份。"""
    parent_fd = _open_directory_no_follow(parent, anchor=parent_anchor)
    trash_fd: int | None = None
    try:
        if FileIdentity.from_stat(os.fstat(parent_fd)) != expected_parent_identity:
            raise CleanupSafetyError(f"Trash 父目录 inode 已变化：{parent}")
        try:
            os.mkdir(trash.name, mode=0o700, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            created = False
        except (PermissionError, OSError) as exc:
            raise CleanupSafetyError(f"无法准备 Trash {trash}：{exc}") from exc

        try:
            path_stat = os.stat(
                trash.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise CleanupSafetyError(f"无法安全检查 Trash {trash}：{exc}") from exc
        _validate_trash_directory_stat(
            trash,
            path_stat,
            expected_device=expected_device,
            uid=uid,
        )
        trash_identity = FileIdentity.from_stat(path_stat)

        no_follow = getattr(os, "O_NOFOLLOW", 0)
        directory_flag = getattr(os, "O_DIRECTORY", 0)
        if not no_follow or not directory_flag:
            raise CleanupSafetyError("当前平台缺少 O_NOFOLLOW/O_DIRECTORY")
        flags = os.O_RDONLY | no_follow | directory_flag
        flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            trash_fd = os.open(trash.name, flags, dir_fd=parent_fd)
        except OSError as exc:
            raise CleanupSafetyError(f"无法无跟随打开 Trash {trash}：{exc}") from exc

        _validate_trash_directory_stat(
            trash,
            os.fstat(trash_fd),
            expected_device=expected_device,
            uid=uid,
            expected_identity=trash_identity,
        )
        if created:
            try:
                os.fchmod(trash_fd, 0o700)
            except OSError as exc:
                raise CleanupSafetyError(
                    f"无法设置 Trash 私有权限 {trash}：{exc}"
                ) from exc
            _validate_trash_directory_stat(
                trash,
                os.fstat(trash_fd),
                expected_device=expected_device,
                uid=uid,
                expected_identity=trash_identity,
            )
    finally:
        if trash_fd is not None:
            try:
                os.close(trash_fd)
            except OSError:
                pass
        try:
            os.close(parent_fd)
        except OSError:
            pass


def _open_directory_no_follow(path: Path, *, anchor: Path) -> int:
    """从可信 anchor 逐组件打开目录，禁止任何中间 symlink。"""
    directory = normalize_path(path)
    trusted_anchor = normalize_path(anchor)
    if not _same_or_descendant(directory, trusted_anchor):
        raise CleanupSafetyError(
            f"目录不在可信锚点下：{directory}（anchor={trusted_anchor}）"
        )
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    if not no_follow or not directory_flag:
        raise CleanupSafetyError("当前平台缺少 O_NOFOLLOW/O_DIRECTORY")
    flags = os.O_RDONLY | no_follow | directory_flag
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(trusted_anchor, flags)
    except OSError as exc:
        raise CleanupSafetyError(
            f"无法安全打开可信锚点 {trusted_anchor}：{exc}"
        ) from exc
    try:
        relative = directory.relative_to(trusted_anchor)
        for component in relative.parts:
            try:
                next_descriptor = os.open(
                    component,
                    flags,
                    dir_fd=descriptor,
                )
            except OSError as exc:
                raise CleanupSafetyError(
                    f"无法无跟随打开目录组件 {component}：{exc}"
                ) from exc
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _stat_at(directory_fd: int, name: str, path: Path) -> os.stat_result:
    try:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise CleanupSafetyError(f"路径已不存在：{path}") from exc
    except OSError as exc:
        raise CleanupSafetyError(f"无法安全检查路径 {path}：{exc}") from exc


def _rename_no_replace(
    source_parent_fd: int,
    source_name: str,
    destination_parent_fd: int,
    destination_name: str,
) -> None:
    """使用 Darwin renameatx_np 原子移动，拒绝覆盖和任意 symlink 解析。"""
    if _RENAMEATX_NP is None:
        raise CleanupSafetyError(
            "当前平台缺少 renameatx_np，无法保证 Trash 目标不被覆盖"
        )
    ctypes.set_errno(0)
    result = _RENAMEATX_NP(
        source_parent_fd,
        os.fsencode(source_name),
        destination_parent_fd,
        os.fsencode(destination_name),
        _RENAME_EXCL | _RENAME_NOFOLLOW_ANY,
    )
    if result != 0:
        error_number = ctypes.get_errno() or errno.EIO
        raise OSError(error_number, os.strerror(error_number))


def _open_audited_parent(
    root_fd: int,
    parent_parts: tuple[str, ...],
    directory_identities: dict[tuple[str, ...], FileIdentity],
) -> int:
    """从已打开的 Trash 根逐层打开并复核审计时目录身份。"""
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    if not no_follow or not directory_flag:
        raise CleanupSafetyError("当前平台缺少 O_NOFOLLOW/O_DIRECTORY")
    flags = os.O_RDONLY | no_follow | directory_flag
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = os.dup(root_fd)
    traversed: tuple[str, ...] = ()
    try:
        for component in parent_parts:
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
            traversed = (*traversed, component)
            expected = directory_identities.get(traversed)
            if (
                expected is None
                or FileIdentity.from_stat(os.fstat(descriptor)) != expected
            ):
                raise CleanupSafetyError(
                    "Trash 审计目录身份已变化：" + "/".join(traversed)
                )
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _move_to_trash(
    item: Item,
    protection: Predicate,
    trash_resolver: Callable[[Path], Path],
    process_runner: Callable[..., subprocess.CompletedProcess[str]] | None,
    uid: int,
    home: Path,
) -> CleanupOutcome:
    assert item.path is not None
    path = normalize_path(item.path)
    _validate_cleanup_scope(item, path, uid, home)
    _validate_startup_item(item, path)
    if item.running_process_markers:
        try:
            process_snapshot = capture_process_snapshot(runner=process_runner)
        except ProcessDetectionError as exc:
            raise CleanupSafetyError(f"执行前进程检测失败：{exc}") from exc
        if process_snapshot.any_running(item.running_process_markers):
            raise CleanupSafetyError(
                "执行前检测到相关工具已启动，拒绝清理"
            )
    if not _identity_matches(path, item):
        raise CleanupSafetyError(f"执行前 inode 已变化：{path}")
    trash = normalize_path(trash_resolver(path))
    live_process_snapshot: ProcessSnapshot | None = None
    live_process_error = ""
    if item.running_process_markers:
        try:
            live_process_snapshot = capture_process_snapshot(
                runner=process_runner
            )
        except ProcessDetectionError as exc:
            live_process_error = f"执行前进程检测失败：{exc}"
    _audit_item(
        item,
        protection,
        home,
        uid,
        live_process_snapshot,
        live_process_error,
    )
    if not _identity_matches(path, item):
        raise CleanupSafetyError(f"准备 Trash 后 inode 已变化：{path}")
    path_stat = _lstat(path)
    if FileFacts(path=path, stat=path_stat).is_probable_cloud_placeholder:
        raise CleanupSafetyError(
            f"候选变成了 macOS dataless/疑似云占位文件：{path}"
        )
    trash_stat = _lstat(trash)
    _validate_trash_directory_stat(
        trash,
        trash_stat,
        expected_device=path_stat.st_dev,
        uid=uid,
    )
    trash_identity = FileIdentity.from_stat(trash_stat)
    if _same_or_descendant(path, trash):
        raise CleanupSafetyError(f"候选已经位于 Trash：{path}")
    destination = _unique_destination(trash, path)
    if not _identity_matches(path, item):
        raise CleanupSafetyError(f"移动前 inode 已变化：{path}")
    _validate_cleanup_scope(item, path, uid, home)
    _validate_startup_item(item, path)
    source_anchor = (
        normalize_path(item.cleanup_root)
        if item.cleanup_scope and item.cleanup_root is not None
        else home
        if _same_or_descendant(path, home)
        else Path("/")
    )
    trash_anchor = home if _same_or_descendant(trash, home) else Path("/")
    source_parent_fd = _open_directory_no_follow(
        path.parent,
        anchor=source_anchor,
    )
    try:
        trash_fd = _open_directory_no_follow(trash, anchor=trash_anchor)
    except Exception:
        try:
            os.close(source_parent_fd)
        except OSError:
            pass
        raise
    close_errors: list[str] = []
    rename_succeeded = False
    try:
        source_stat = _stat_at(source_parent_fd, path.name, path)
        if stat.S_ISLNK(source_stat.st_mode):
            raise CleanupSafetyError(f"候选根路径变成了符号链接：{path}")
        source_facts = FileFacts(path=path, stat=source_stat)
        if source_facts.is_probable_cloud_placeholder:
            raise CleanupSafetyError(
                f"移动前候选变成了 macOS dataless/疑似云占位文件：{path}"
            )
        source_identity = source_facts.identity
        if item.identity is not None and source_identity != item.identity:
            raise CleanupSafetyError(f"移动前 inode 已变化：{path}")
        _validate_trash_directory_stat(
            trash,
            os.fstat(trash_fd),
            expected_device=source_stat.st_dev,
            uid=uid,
            expected_identity=trash_identity,
        )
        try:
            os.stat(destination.name, dir_fd=trash_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise CleanupSafetyError(f"Trash 目标已被占用：{destination}")
        try:
            _rename_no_replace(
                source_parent_fd,
                path.name,
                trash_fd,
                destination.name,
            )
            rename_succeeded = True
        except (PermissionError, OSError) as exc:
            raise CleanupSafetyError(f"移动到 Trash 失败 {path}：{exc}") from exc
        try:
            destination_stat = _stat_at(trash_fd, destination.name, destination)
        except CleanupSafetyError as exc:
            return CleanupOutcome(
                item=item,
                status="partial",
                destination=destination,
                message=(
                    "Trash rename 已成功，但无法确认移动后目标；"
                    f"原操作已经发生：{exc}"
                ),
            )
        if (
            item.identity is not None
            and FileFacts(path=destination, stat=destination_stat).identity
            != item.identity
        ):
            return CleanupOutcome(
                item=item,
                status="partial",
                destination=destination,
                message=(
                    "Trash rename 已成功，但目标 inode 在复核前发生变化；"
                    "原操作已经发生"
                ),
            )
        source_note = ""
        try:
            recreated_stat = os.stat(
                path.name,
                dir_fd=source_parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        except OSError as exc:
            source_note = f"；移动后无法复核源路径：{exc}"
        else:
            recreated_identity = FileIdentity.from_stat(recreated_stat)
            kind = "原 inode 的新链接" if recreated_identity == item.identity else "新对象"
            source_note = f"；源路径已出现{kind}，未对其执行清理"
    finally:
        for label, descriptor in (
            ("源目录", source_parent_fd),
            ("Trash 目录", trash_fd),
        ):
            try:
                os.close(descriptor)
            except OSError as exc:
                close_errors.append(f"{label}: {exc}")
    if close_errors and rename_succeeded:
        return CleanupOutcome(
            item=item,
            status="partial",
            destination=destination,
            message=(
                "Trash rename 已成功，但关闭目录描述符失败；"
                f"原操作已经发生：{'；'.join(close_errors)}"
            ),
        )
    return CleanupOutcome(
        item=item,
        status="moved_to_trash",
        bytes_affected=item.size,
        destination=destination,
        message=f"已移动到同卷 Trash；空间尚未实际释放{source_note}",
    )


def _empty_trash(
    item: Item,
    *,
    protection: Predicate,
    home: Path,
    uid: int,
) -> CleanupOutcome:
    assert item.path is not None
    root = normalize_path(item.path)
    _audit_item(item, protection, home, uid, None, "")
    _validate_cleanup_scope(item, root, uid, home)
    if not _identity_matches(root, item):
        raise CleanupSafetyError(f"执行前 inode 已变化：{root}")
    anchor = home if _same_or_descendant(root, home) else Path("/")
    root_fd = _open_directory_no_follow(root, anchor=anchor)
    root_stat = os.fstat(root_fd)
    if not stat.S_ISDIR(root_stat.st_mode):
        os.close(root_fd)
        raise CleanupSafetyError(f"Trash 根目录不是目录：{root}")
    if (
        item.identity is not None
        and FileFacts(path=root, stat=root_stat).identity != item.identity
    ):
        os.close(root_fd)
        raise CleanupSafetyError(f"Trash 根目录 inode 已变化：{root}")
    root_identity = FileIdentity.from_stat(root_stat)
    try:
        snapshot = _audit_descendants(
            root,
            root_stat.st_dev,
            protection,
            uid,
            expected_root_identity=root_identity,
        )
    except Exception:
        os.close(root_fd)
        raise
    directory_identities = {
        entry.relative_parts: entry.identity
        for entry in snapshot
        if entry.is_directory
    }
    failures: list[str] = []
    deleted_count = 0
    disappeared_count = 0
    live_root_stat = os.fstat(root_fd)
    if FileFacts(path=root, stat=live_root_stat).is_dataless:
        os.close(root_fd)
        raise CleanupSafetyError(f"Trash 根目录变成了 macOS dataless 目录：{root}")
    for entry in snapshot:
        relative_path = Path(*entry.relative_parts)
        parent_fd: int | None = None
        try:
            parent_fd = _open_audited_parent(
                root_fd,
                entry.relative_parts[:-1],
                directory_identities,
            )
            name = entry.relative_parts[-1]
            try:
                current_stat = os.stat(
                    name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                disappeared_count += 1
                continue
            if FileIdentity.from_stat(current_stat) != entry.identity:
                raise CleanupSafetyError(
                    f"审计后 inode 已变化：{root / relative_path}"
                )
            if stat.S_ISDIR(current_stat.st_mode) != entry.is_directory:
                raise CleanupSafetyError(
                    f"审计后类型已变化：{root / relative_path}"
                )
            if entry.is_directory:
                os.rmdir(name, dir_fd=parent_fd)
            else:
                os.unlink(name, dir_fd=parent_fd)
            deleted_count += 1
        except FileNotFoundError:
            disappeared_count += 1
        except (CleanupSafetyError, PermissionError, OSError) as exc:
            failures.append(f"{root / relative_path}: {exc}")
        finally:
            if parent_fd is not None:
                os.close(parent_fd)

    remaining: list[str] | None
    try:
        live_root_stat = os.fstat(root_fd)
        if FileFacts(path=root, stat=live_root_stat).is_dataless:
            raise CleanupSafetyError(
                f"Trash 根目录变成了 macOS dataless 目录：{root}"
            )
        with os.scandir(root_fd) as iterator:
            remaining = sorted(entry.name for entry in iterator)
    except CleanupSafetyError:
        raise
    except (PermissionError, FileNotFoundError, OSError) as exc:
        failures.append(f"复核失败：{exc}")
        remaining = None
    finally:
        os.close(root_fd)

    audited_root_names = {
        entry.relative_parts[0]
        for entry in snapshot
        if len(entry.relative_parts) == 1
    }
    remaining_names = set(remaining or ())
    audited_remaining = sorted(remaining_names & audited_root_names)
    new_remaining = sorted(remaining_names - audited_root_names)
    if audited_remaining:
        failures.append(f"仍有 {len(audited_remaining)} 个已审计顶层项未删除")

    summary = f"已永久删除 {deleted_count}/{len(snapshot)} 个审计对象"
    if disappeared_count:
        summary += f"；{disappeared_count} 个对象在删除前已不存在"
    if new_remaining:
        summary += f"；保留审计后新增 {len(new_remaining)} 个顶层项"
    if failures:
        status = "partial" if deleted_count else "failed"
        return CleanupOutcome(
            item=item,
            status=status,
            bytes_affected=0,
            message=(
                f"{summary}；{'；'.join(failures[:5])}；"
                "部分结果的实际释放容量无法可靠确定"
            ),
        )
    return CleanupOutcome(
        item=item,
        status="deleted",
        bytes_affected=(item.size if deleted_count == len(snapshot) else 0),
        message=summary,
    )


def _prune_docker_item(
    item: Item,
    *,
    docker_path: str | None,
    docker_runner: Callable[..., subprocess.CompletedProcess[str]] | None,
    docker_finder: Callable[[str], str | None] | None,
) -> CleanupOutcome:
    result = prune_docker_resource(
        item.identifier,
        resource_binding=item.resource_binding,
        docker_path=docker_path,
        runner=docker_runner,
        finder=docker_finder,
    )
    return CleanupOutcome(
        item=item,
        status="pruned",
        bytes_affected=result.reclaimed_bytes,
        message=result.message,
    )


def execute_cleanup(
    items: Iterable[Item],
    protection: Predicate,
    *,
    home: Path | None = None,
    uid: int | None = None,
    trash_resolver: Callable[[Path], Path] | None = None,
    docker_path: str | None = None,
    docker_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    docker_finder: Callable[[str], str | None] | None = None,
    process_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> CleanupReport:
    """先批量预检，全部通过后再逐项执行，并在每项操作前复核 inode。"""
    selected = list(items)
    if not selected:
        return CleanupReport()
    home = normalize_path(home or Path.home())
    uid = os.getuid() if uid is None else uid
    process_snapshot: ProcessSnapshot | None = None
    process_error = ""
    if any(item.running_process_markers for item in selected):
        try:
            process_snapshot = capture_process_snapshot(runner=process_runner)
        except ProcessDetectionError as exc:
            process_error = f"进程检测失败，拒绝清理受运行状态保护的候选：{exc}"
    preflight_errors: dict[tuple[str, str, str, str], str] = {}
    for item in selected:
        try:
            _audit_item(
                item,
                protection,
                home,
                uid,
                process_snapshot,
                process_error,
            )
        except CleanupSafetyError as exc:
            preflight_errors[_item_key(item)] = str(exc)

    if preflight_errors:
        return CleanupReport(
            outcomes=[
                CleanupOutcome(
                    item=item,
                    status=(
                        "blocked"
                        if _item_key(item) in preflight_errors
                        else "not_run"
                    ),
                    message=preflight_errors.get(
                        _item_key(item), "批次因其他候选预检失败而取消"
                    ),
                )
                for item in selected
            ]
        )

    resolver = trash_resolver or (
        lambda path: trash_directory_for(path, home=home, uid=uid)
    )
    report = CleanupReport()
    for item in selected:
        try:
            if item.resource_kind == "docker":
                outcome = _prune_docker_item(
                    item,
                    docker_path=docker_path,
                    docker_runner=docker_runner,
                    docker_finder=docker_finder,
                )
            elif item.domain == "trash":
                outcome = _empty_trash(
                    item,
                    protection=protection,
                    home=home,
                    uid=uid,
                )
            else:
                outcome = _move_to_trash(
                    item,
                    protection,
                    resolver,
                    process_runner,
                    uid,
                    home,
                )
        except DockerPruneError as exc:
            unknown_note = (
                "；Docker prune 的不可逆副作用可能已经发生；"
                "实际释放容量未知"
                if exc.side_effect_unknown
                else ""
            )
            outcome = CleanupOutcome(
                item=item,
                status=("partial" if exc.side_effect_unknown else "failed"),
                message=f"{exc}{unknown_note}",
            )
        except (CleanupSafetyError, OSError) as exc:
            outcome = CleanupOutcome(
                item=item,
                status="failed",
                message=str(exc),
            )
        report.outcomes.append(outcome)
    return report
