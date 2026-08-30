"""运行中进程的只读快照，用于避免清理正在使用的工具缓存。"""
from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PROCESS_TIMEOUT = 5.0
DEFAULT_OPEN_FILE_TIMEOUT = 10.0


class ProcessDetectionError(RuntimeError):
    pass


class OpenFileDetectionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProcessSnapshot:
    commands: tuple[str, ...]

    def matching_commands(self, markers: Iterable[str]) -> tuple[str, ...]:
        normalized = tuple(
            marker.strip().casefold() for marker in markers if marker.strip()
        )
        if not normalized:
            return ()
        return tuple(
            command
            for command in self.commands
            if any(marker in command.casefold() for marker in normalized)
        )

    def any_running(self, markers: Iterable[str]) -> bool:
        return bool(self.matching_commands(markers))


@dataclass(frozen=True)
class OpenFileSnapshot:
    """`lsof` 报告的打开路径快照；不读取文件内容或进程环境。"""

    paths: tuple[str, ...]

    def count_under(self, root: str | os.PathLike[str]) -> int:
        normalized = os.path.abspath(os.fspath(root))
        prefix = normalized.rstrip(os.sep) + os.sep
        return sum(
            candidate == normalized or candidate.startswith(prefix)
            for candidate in self.paths
        )

    def count_sqlite_family(self, database: str | os.PathLike[str]) -> int:
        normalized = os.path.abspath(os.fspath(database))
        family = {
            normalized,
            f"{normalized}-journal",
            f"{normalized}-shm",
            f"{normalized}-wal",
        }
        return sum(candidate in family for candidate in self.paths)


@dataclass(frozen=True)
class DeletedOpenFile:
    """一个已 unlink、但仍被至少一个进程持有的文件事实。"""

    device: int
    inode: int
    logical_size: int
    handle_count: int
    commands: tuple[str, ...]


@dataclass(frozen=True)
class DeletedOpenFileSnapshot:
    """按 device/inode 去重后的 deleted-open 文件快照。"""

    files: tuple[DeletedOpenFile, ...]


@dataclass
class _DeletedOpenAggregate:
    logical_size: int = 0
    handle_count: int = 0
    commands: set[str] = field(default_factory=set)


def _lsof_integer(value: str) -> int | None:
    try:
        return int(value, 0)
    except ValueError:
        try:
            return int(value, 10)
        except ValueError:
            return None


def parse_deleted_open_files(output: str) -> DeletedOpenFileSnapshot:
    """解析 ``lsof +L1 -FpcfDis``，不保留文件路径或完整命令行。"""

    aggregates: dict[tuple[int, int], _DeletedOpenAggregate] = {}
    command = ""
    in_file_record = False
    device: int | None = None
    inode: int | None = None
    logical_size: int | None = None

    def invalid_output(reason: str) -> OpenFileDetectionError:
        return OpenFileDetectionError(
            f"无法解析 lsof +L1 字段输出：{reason}"
        )

    def flush_file() -> None:
        if not in_file_record:
            return
        if device is None or inode is None or logical_size is None:
            raise invalid_output("文件记录缺少 device、inode 或 size")
        aggregate = aggregates.setdefault(
            (device, inode),
            _DeletedOpenAggregate(),
        )
        aggregate.logical_size = max(aggregate.logical_size, logical_size)
        aggregate.handle_count += 1
        if command:
            aggregate.commands.add(command)

    for raw_line in output.splitlines():
        if not raw_line:
            continue
        field, value = raw_line[0], raw_line[1:]
        if field == "p":
            flush_file()
            in_file_record = False
            device = inode = None
            logical_size = None
            command = ""
        elif field == "c":
            command = " ".join(value.split())[:128]
        elif field == "f":
            flush_file()
            in_file_record = True
            device = inode = None
            logical_size = None
        elif field == "D":
            if not in_file_record:
                raise invalid_output("device 字段不在文件记录内")
            device = _lsof_integer(value)
            if device is None or device < 0:
                raise invalid_output("device 字段无效")
        elif field == "i":
            if not in_file_record:
                raise invalid_output("inode 字段不在文件记录内")
            inode = _lsof_integer(value)
            if inode is None or inode < 0:
                raise invalid_output("inode 字段无效")
        elif field == "s":
            if not in_file_record:
                raise invalid_output("size 字段不在文件记录内")
            logical_size = _lsof_integer(value)
            if logical_size is None or logical_size < 0:
                raise invalid_output("size 字段无效")
    flush_file()

    return DeletedOpenFileSnapshot(
        tuple(
            DeletedOpenFile(
                device=device_id,
                inode=inode_id,
                logical_size=aggregate.logical_size,
                handle_count=aggregate.handle_count,
                commands=tuple(sorted(aggregate.commands)),
            )
            for (device_id, inode_id), aggregate in sorted(aggregates.items())
        )
    )


def capture_process_snapshot(
    *,
    timeout: float = DEFAULT_PROCESS_TIMEOUT,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> ProcessSnapshot:
    """通过 macOS `ps` 获取命令行，不读取进程内存或环境变量。"""
    run = runner or subprocess.run
    command = ["/bin/ps", "-axo", "command="]
    try:
        completed = run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProcessDetectionError(
            f"进程检测在 {timeout:g} 秒内未完成"
        ) from exc
    except OSError as exc:
        raise ProcessDetectionError(f"无法启动进程检测：{exc}") from exc
    if completed.returncode != 0:
        message = " ".join((completed.stderr or "").strip().split())
        raise ProcessDetectionError(
            message or f"ps 退出码 {completed.returncode}"
        )
    return ProcessSnapshot(
        commands=tuple(
            line.strip()
            for line in completed.stdout.splitlines()
            if line.strip()
        )
    )


def capture_open_file_snapshot(
    *,
    timeout: float = DEFAULT_OPEN_FILE_TIMEOUT,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> OpenFileSnapshot:
    """通过 macOS `lsof -F n` 获取打开路径，不读取文件正文。"""

    run = runner or subprocess.run
    command = ["/usr/sbin/lsof", "-nP", "-Fn"]
    try:
        completed = run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise OpenFileDetectionError(
            f"打开句柄检测在 {timeout:g} 秒内未完成"
        ) from exc
    except OSError as exc:
        raise OpenFileDetectionError(f"无法启动打开句柄检测：{exc}") from exc
    if completed.returncode != 0:
        message = " ".join((completed.stderr or "").strip().split())
        raise OpenFileDetectionError(
            message or f"lsof 退出码 {completed.returncode}"
        )
    paths = tuple(
        line[1:]
        for line in completed.stdout.splitlines()
        if line.startswith("n") and Path(line[1:]).is_absolute()
    )
    return OpenFileSnapshot(paths)


def capture_deleted_open_file_snapshot(
    *,
    timeout: float = DEFAULT_OPEN_FILE_TIMEOUT,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> DeletedOpenFileSnapshot:
    """读取 deleted-open 文件，不采集路径、参数或进程环境。"""

    run = runner or subprocess.run
    command = [
        "/usr/sbin/lsof",
        "-nP",
        "+L1",
        "-FpcfDis",
    ]
    try:
        completed = run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise OpenFileDetectionError(
            f"deleted-open 检测在 {timeout:g} 秒内未完成"
        ) from exc
    except OSError as exc:
        raise OpenFileDetectionError(
            f"无法启动 deleted-open 检测：{exc}"
        ) from exc
    stderr = " ".join((completed.stderr or "").strip().split())
    if completed.returncode != 0 and not (
        completed.returncode == 1
        and not completed.stdout.strip()
        and not stderr
    ):
        raise OpenFileDetectionError(
            stderr or f"lsof +L1 退出码 {completed.returncode}"
        )
    snapshot = parse_deleted_open_files(completed.stdout)
    if completed.stdout.strip() and not snapshot.files:
        raise OpenFileDetectionError("无法解析 lsof +L1 字段输出")
    return snapshot
