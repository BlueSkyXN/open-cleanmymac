"""macOS 运行时路径发现服务。"""
from __future__ import annotations

import fnmatch
import os
import stat
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .models import FileFacts, ScanIssue, normalize_path

SYSTEM_PROTECTED_ROOTS = tuple(
    Path(path)
    for path in (
        "/System",
        "/Library",
        "/Applications",
        "/Users",
        "/opt",
        "/bin",
        "/sbin",
        "/usr",
        "/private",
        "/etc",
        "/var",
        "/dev",
        "/home",
        "/net",
    )
)
TRUSTED_SCAN_ALIAS_ROOTS = (Path("/var/folders"),)
# Darwin sys/unistd.h；Python 的 pathconf_names 未必暴露此扩展。
_MACOS_PC_CASE_SENSITIVE = 11


def filesystem_case_sensitive(path: Path) -> bool:
    if sys.platform != "darwin":
        raise OSError("无法查询非 macOS 文件系统的大小写语义")
    value = os.pathconf(path, _MACOS_PC_CASE_SENSITIVE)
    if value not in (0, 1):
        raise OSError("文件系统大小写语义未知")
    return bool(value)


def filesystem_name_matches(
    path: Path,
    patterns: tuple[str, ...],
    *,
    probe: Callable[[Path], FileFacts | None],
) -> bool:
    if any(fnmatch.fnmatchcase(path.name, pattern) for pattern in patterns):
        return True
    if not any(fnmatch.fnmatchcase(path.name.casefold(), pattern.casefold()) for pattern in patterns):
        return False
    parent = probe(path.parent)
    if parent is None or not stat.S_ISDIR(parent.stat.st_mode):
        raise OSError("无法确认名称所属目录的大小写语义")
    return not filesystem_case_sensitive(path.parent)


def filesystem_relative_parts(
    path: Path,
    root: Path,
    *,
    probe: Callable[[Path], FileFacts | None],
) -> tuple[str, ...] | None:
    parts, root_parts = path.parts, root.parts
    if len(parts) < len(root_parts):
        return None
    if parts[:len(root_parts)] == root_parts:
        return parts[len(root_parts):]
    if any(left.casefold() != right.casefold() for left, right in zip(parts, root_parts)):
        return None
    for index, (left, right) in enumerate(zip(parts, root_parts)):
        if left == right:
            continue
        parent = Path(*parts[:index])
        facts = probe(parent)
        if facts is None or not stat.S_ISDIR(facts.stat.st_mode):
            raise OSError("无法确认范围所属目录的大小写语义")
        if filesystem_case_sensitive(parent):
            return None
        actual, expected = probe(parent / left), probe(parent / right)
        if actual is None or expected is None:
            return None
        # 大小写折叠只筛选可能的别名，不能代替 no-follow 身份核对。
        if actual.identity != expected.identity:
            return None
    actual, expected = probe(Path(*parts[:len(root_parts)])), probe(root)
    if actual is None or expected is None or actual.identity != expected.identity:
        return None
    return parts[len(root_parts):]


def _same_or_descendant(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((str(path), str(root))) == str(root)
    except ValueError:
        return False


def symlink_component(
    path: str | os.PathLike[str],
    *,
    anchor: str | os.PathLike[str],
) -> Path | None:
    """返回 anchor 到 path 间首个 symlink 组件；路径不在 anchor 下时返回空。"""
    candidate = normalize_path(path)
    trusted_anchor = normalize_path(anchor)
    if not _same_or_descendant(candidate, trusted_anchor):
        return None
    try:
        relative = candidate.relative_to(trusted_anchor)
    except ValueError:
        return None

    current = trusted_anchor
    components = [current]
    for part in relative.parts:
        current /= part
        components.append(current)
    for component in components:
        try:
            stat_result = component.lstat()
        except OSError:
            return None
        if stat.S_ISLNK(stat_result.st_mode):
            return component
    return None


def scan_symlink_anchor(
    path: str | os.PathLike[str], *, home: Path | None = None
) -> Path:
    """选择扫描期 symlink 检查锚点，并保留 macOS 受信系统别名。"""
    candidate = normalize_path(path)
    user_home = normalize_path(home or Path.home())
    if _same_or_descendant(candidate, user_home):
        return user_home
    for root in TRUSTED_SCAN_ALIAS_ROOTS:
        if _same_or_descendant(candidate, root):
            return root
    return Path("/")


def nonprivileged_action_block_reason(
    path: str | os.PathLike[str], *, home: Path | None = None
) -> str:
    """返回用户态清理的结构性拒绝理由；空字符串表示可继续复核。"""
    candidate = normalize_path(path)
    home = normalize_path(home or Path.home())
    within_home = candidate != home and _same_or_descendant(candidate, home)
    if candidate == home or _same_or_descendant(home, candidate):
        return f"用户目录或其祖先：{candidate}"
    if candidate in {Path("/"), Path("/Volumes")} or (
        not within_home
        and any(
            candidate == root or _same_or_descendant(candidate, root)
            for root in SYSTEM_PROTECTED_ROOTS
        )
    ):
        return f"系统保护路径：{candidate}"
    if os.path.ismount(candidate):
        return f"挂载点：{candidate}"
    return ""


@dataclass(frozen=True)
class TrashDiscovery:
    paths: tuple[Path, ...]
    issues: tuple[ScanIssue, ...] = ()


@dataclass(frozen=True)
class DarwinUserCacheDiscovery:
    paths: tuple[Path, ...] = ()
    issues: tuple[ScanIssue, ...] = ()


@dataclass(frozen=True)
class DarwinUserTempDiscovery:
    paths: tuple[Path, ...] = ()
    issues: tuple[ScanIssue, ...] = ()


@dataclass(frozen=True)
class LocalSnapshotDiscovery:
    mount_point: Path
    snapshots: tuple[str, ...] = ()
    issues: tuple[ScanIssue, ...] = ()


def _discover_darwin_user_directory(
    variable: str,
    task: str,
    *,
    getconf_path: str = "/usr/bin/getconf",
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    timeout: float = 2.0,
) -> tuple[tuple[Path, ...], tuple[ScanIssue, ...]]:
    command = [getconf_path, variable]
    run = runner or subprocess.run
    try:
        completed = run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        message = f"未找到 getconf：{exc}"
    except subprocess.TimeoutExpired:
        message = f"getconf 在 {timeout:g} 秒内未完成"
    except OSError as exc:
        message = f"无法运行 getconf：{exc}"
    else:
        if completed.returncode != 0:
            detail = completed.stderr.strip()
            message = detail or f"getconf 退出码 {completed.returncode}"
        else:
            lines = [
                line.strip()
                for line in completed.stdout.splitlines()
                if line.strip()
            ]
            if len(lines) != 1 or "\x00" in lines[0]:
                message = f"getconf 返回的 {variable} 路径格式无效"
            else:
                raw_path = lines[0]
                if not os.path.isabs(raw_path):
                    message = f"getconf 返回的 {variable} 路径不是绝对路径"
                else:
                    path = normalize_path(raw_path)
                    if path == Path("/"):
                        message = "getconf 返回了不安全的根目录"
                    else:
                        return (path,), ()

    return (), (
        ScanIssue(
            code="path_discovery_failed",
            message=message,
            task=task,
        ),
    )


def discover_darwin_user_cache(
    *,
    getconf_path: str = "/usr/bin/getconf",
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    timeout: float = 2.0,
) -> DarwinUserCacheDiscovery:
    """通过 macOS 公开 ``getconf`` 接口发现当前用户的 Darwin cache 根。"""

    paths, issues = _discover_darwin_user_directory(
        "DARWIN_USER_CACHE_DIR",
        "Darwin 用户缓存",
        getconf_path=getconf_path,
        runner=runner,
        timeout=timeout,
    )
    return DarwinUserCacheDiscovery(paths, issues)


def discover_darwin_user_temp(
    *,
    getconf_path: str = "/usr/bin/getconf",
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    timeout: float = 2.0,
) -> DarwinUserTempDiscovery:
    """通过公开 ``getconf`` 接口发现当前用户的 Darwin temp 根。"""

    paths, issues = _discover_darwin_user_directory(
        "DARWIN_USER_TEMP_DIR",
        "Darwin updater 临时副本",
        getconf_path=getconf_path,
        runner=runner,
        timeout=timeout,
    )
    return DarwinUserTempDiscovery(paths, issues)


def volume_mount_point(path: str | os.PathLike[str]) -> Path:
    """按设备号向上定位当前路径所在卷的挂载根。"""
    current = normalize_path(path)
    current_stat = current.lstat()
    device = current_stat.st_dev
    while current.parent != current:
        parent = current.parent
        if parent.lstat().st_dev != device:
            break
        current = parent
    return current


def discover_local_snapshots(
    mount_point: str | os.PathLike[str],
    *,
    tmutil_path: str = "/usr/bin/tmutil",
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    timeout: float = 5.0,
) -> LocalSnapshotDiscovery:
    """只读列出指定卷的 Time Machine 本地快照，不执行删除。"""
    mount = normalize_path(mount_point)
    command = [tmutil_path, "listlocalsnapshots", str(mount)]
    run = runner or subprocess.run
    try:
        completed = run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        message = f"未找到 tmutil：{exc}"
    except subprocess.TimeoutExpired:
        message = f"tmutil 在 {timeout:g} 秒内未完成"
    except OSError as exc:
        message = f"无法运行 tmutil：{exc}"
    else:
        if completed.returncode == 0:
            snapshots = tuple(
                sorted(
                    {
                        line.strip()
                        for line in completed.stdout.splitlines()
                        if line.strip().startswith("com.apple.TimeMachine.")
                    }
                )
            )
            return LocalSnapshotDiscovery(
                mount_point=mount,
                snapshots=snapshots,
            )
        detail = completed.stderr.strip()
        message = detail or f"tmutil 退出码 {completed.returncode}"

    return LocalSnapshotDiscovery(
        mount_point=mount,
        issues=(
            ScanIssue(
                code="snapshot_discovery_failed",
                message=message,
                task="time-machine-local-snapshots",
                path=mount,
                blocking=False,
            ),
        ),
    )


def discover_trash_paths(
    *,
    home: Path | None = None,
    volumes_root: Path = Path("/Volumes"),
    uid: int | None = None,
) -> TrashDiscovery:
    """发现当前用户主卷和所有挂载卷的 Trash 路径。"""
    user_home = normalize_path(home or Path.home())
    user_id = os.getuid() if uid is None else uid
    paths: set[Path] = {user_home / ".Trash"}
    issues: list[ScanIssue] = []
    normalized_volumes = normalize_path(volumes_root)

    try:
        with os.scandir(normalized_volumes) as iterator:
            entries = list(iterator)
    except FileNotFoundError:
        entries = []
    except (PermissionError, OSError) as exc:
        issues.append(
            ScanIssue(
                code=(
                    "permission_denied"
                    if isinstance(exc, PermissionError)
                    else "filesystem_error"
                ),
                message=str(exc),
                task="trash-discovery",
                path=normalized_volumes,
            )
        )
        entries = []

    for entry in entries:
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
        except OSError as exc:
            issues.append(
                ScanIssue(
                    code=(
                        "permission_denied"
                        if isinstance(exc, PermissionError)
                        else "filesystem_error"
                    ),
                    message=str(exc),
                    task="trash-discovery",
                    path=normalize_path(entry.path),
                )
            )
            continue
        paths.add(normalize_path(entry.path) / ".Trashes" / str(user_id))

    return TrashDiscovery(
        paths=tuple(sorted(paths, key=str)),
        issues=tuple(issues),
    )
