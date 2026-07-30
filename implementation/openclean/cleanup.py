"""清理选择、安全预检与用户态执行。

普通文件系统候选仅移动到同一文件系统的 Trash；只有已经位于 Trash 根目录中的
内容才会永久删除。Docker 仅执行代码内审计过的 prune 白名单；特权路径与
Docker Local Volumes 保持不可执行。
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path

from .docker import (
    DockerPruneError,
    docker_prune_supported,
    prune_docker_resource,
)
from .macos import (
    discover_darwin_user_cache,
    nonprivileged_action_block_reason,
    symlink_component,
)
from .models import FileFacts, Item, ScanResult, normalize_path
from .predicates import Predicate, ProtectionGate
from .processes import (
    ProcessDetectionError,
    ProcessSnapshot,
    capture_process_snapshot,
)
from .startup_items import StartupItemError, startup_item_still_broken


class SelectionError(ValueError):
    pass


class CleanupSafetyError(RuntimeError):
    pass


_SUCCESS_STATUSES = frozenset({"moved_to_trash", "deleted", "pruned"})


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


def _audit_descendants(
    root: Path,
    root_device: int,
    protection: Predicate,
    expected_uid: int,
) -> None:
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

    stack = [root]
    while stack:
        directory = stack.pop()
        directory_stat = _lstat(directory)
        validate_directory(directory, directory_stat)
        directory_fd = _open_directory_no_follow(directory, anchor=root)
        try:
            live_stat = os.fstat(directory_fd)
            validate_directory(directory, live_stat)
            with os.scandir(directory_fd) as iterator:
                entries = list(iterator)
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
            if stat.S_ISLNK(stat_result.st_mode):
                continue
            if stat.S_ISDIR(stat_result.st_mode):
                if stat_result.st_dev != root_device:
                    raise CleanupSafetyError(f"目录包含其他文件系统：{path}")
                stack.append(path)


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
        created = not os.path.lexists(trash)
        try:
            trash.mkdir(mode=0o700, exist_ok=True)
        except (PermissionError, OSError) as exc:
            raise CleanupSafetyError(f"无法准备用户 Trash {trash}：{exc}") from exc
        if created:
            trash.chmod(0o700)
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
        created = not os.path.lexists(trash)
        try:
            trash.mkdir(mode=0o700, exist_ok=True)
        except (PermissionError, OSError) as exc:
            raise CleanupSafetyError(f"无法准备卷 Trash {trash}：{exc}") from exc
        if created:
            trash.chmod(0o700)

    trash_stat = _lstat(trash)
    if stat.S_ISLNK(trash_stat.st_mode) or not stat.S_ISDIR(trash_stat.st_mode):
        raise CleanupSafetyError(f"Trash 路径无效：{trash}")
    if trash_stat.st_dev != candidate_stat.st_dev:
        raise CleanupSafetyError(f"Trash 与候选不在同一文件系统：{trash}")
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
    if stat.S_ISLNK(trash_stat.st_mode) or not stat.S_ISDIR(
        trash_stat.st_mode
    ):
        raise CleanupSafetyError(f"Trash 路径无效：{trash}")
    if path_stat.st_dev != trash_stat.st_dev:
        raise CleanupSafetyError(f"Trash 与候选不在同一文件系统：{trash}")
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
    trash_fd = _open_directory_no_follow(trash, anchor=trash_anchor)
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
        if source_stat.st_dev != os.fstat(trash_fd).st_dev:
            raise CleanupSafetyError(f"Trash 与候选不在同一文件系统：{trash}")
        try:
            os.stat(destination.name, dir_fd=trash_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise CleanupSafetyError(f"Trash 目标已被占用：{destination}")
        try:
            os.rename(
                path.name,
                destination.name,
                src_dir_fd=source_parent_fd,
                dst_dir_fd=trash_fd,
            )
        except (PermissionError, OSError) as exc:
            raise CleanupSafetyError(f"移动到 Trash 失败 {path}：{exc}") from exc
        try:
            os.stat(path.name, dir_fd=source_parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise CleanupSafetyError(f"移动后源路径仍然存在：{path}")
        destination_stat = _stat_at(trash_fd, destination.name, destination)
        if (
            item.identity is not None
            and FileFacts(path=destination, stat=destination_stat).identity
            != item.identity
        ):
            raise CleanupSafetyError(f"移动后 inode 校验失败：{destination}")
    finally:
        os.close(source_parent_fd)
        os.close(trash_fd)
    return CleanupOutcome(
        item=item,
        status="moved_to_trash",
        bytes_affected=item.size,
        destination=destination,
        message="已移动到同卷 Trash；空间尚未实际释放",
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
    if not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
        os.close(root_fd)
        raise CleanupSafetyError("当前 Python 不支持抗符号链接攻击的目录删除")

    _audit_descendants(root, root_stat.st_dev, protection, uid)

    failures: list[str] = []
    live_root_stat = os.fstat(root_fd)
    if FileFacts(path=root, stat=live_root_stat).is_dataless:
        os.close(root_fd)
        raise CleanupSafetyError(f"Trash 根目录变成了 macOS dataless 目录：{root}")
    try:
        with os.scandir(root_fd) as iterator:
            entries = list(iterator)
    except (PermissionError, FileNotFoundError, OSError) as exc:
        os.close(root_fd)
        raise CleanupSafetyError(f"无法枚举 Trash {root}：{exc}") from exc
    for entry in entries:
        try:
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.name, dir_fd=root_fd)
            else:
                os.unlink(entry.name, dir_fd=root_fd)
        except (PermissionError, FileNotFoundError, OSError) as exc:
            failures.append(f"{root / entry.name}: {exc}")

    try:
        live_root_stat = os.fstat(root_fd)
        if FileFacts(path=root, stat=live_root_stat).is_dataless:
            raise CleanupSafetyError(
                f"Trash 根目录变成了 macOS dataless 目录：{root}"
            )
        with os.scandir(root_fd) as iterator:
            remaining = [entry.name for entry in iterator]
    except CleanupSafetyError:
        raise
    except (PermissionError, FileNotFoundError, OSError) as exc:
        failures.append(f"复核失败：{exc}")
        remaining = []
    finally:
        os.close(root_fd)
    if remaining:
        failures.append(f"仍有 {len(remaining)} 项未删除")
    if failures:
        raise CleanupSafetyError("；".join(failures[:5]))
    return CleanupOutcome(
        item=item,
        status="deleted",
        bytes_affected=item.size,
        message="Trash 内容已永久删除",
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
        except (CleanupSafetyError, DockerPruneError, OSError) as exc:
            outcome = CleanupOutcome(
                item=item,
                status="failed",
                message=str(exc),
            )
        report.outcomes.append(outcome)
    return report
