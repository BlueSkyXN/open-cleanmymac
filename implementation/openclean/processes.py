"""运行中进程的只读快照，用于避免清理正在使用的工具缓存。"""
from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass

DEFAULT_PROCESS_TIMEOUT = 5.0


class ProcessDetectionError(RuntimeError):
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
