"""`analyze` 的只读终端目录浏览与 Finder reveal。"""
from __future__ import annotations

import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from .analyzer import AnalyzeError, SpaceAnalysis, analyze_path
from .engine import Control, human
from .predicates import Predicate


class RevealError(RuntimeError):
    pass


def reveal_in_finder(
    path: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    timeout: float = 10.0,
) -> None:
    """使用 macOS `open -R` 在 Finder 中选中路径。"""
    try:
        stat_result = path.lstat()
    except FileNotFoundError as exc:
        raise RevealError(f"路径已不存在：{path}") from exc
    except (PermissionError, OSError) as exc:
        raise RevealError(f"无法检查路径 {path}：{exc}") from exc
    if stat.S_ISLNK(stat_result.st_mode):
        raise RevealError(f"拒绝 reveal 符号链接：{path}")

    run = runner or subprocess.run
    command = ["/usr/bin/open", "-R", str(path)]
    try:
        completed = run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RevealError(f"Finder 在 {timeout:g} 秒内未响应") from exc
    except OSError as exc:
        raise RevealError(f"无法启动 Finder reveal：{exc}") from exc
    if completed.returncode != 0:
        message = " ".join((completed.stderr or "").strip().split())
        raise RevealError(
            message or f"open -R 退出码 {completed.returncode}"
        )


def _is_directory(path: Path) -> bool:
    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except (PermissionError, FileNotFoundError, OSError):
        return False


def _print_analysis(
    analysis: SpaceAnalysis,
    *,
    top: int,
    output: TextIO,
    error: TextIO,
) -> list:
    entries = analysis.entries[:top] if top else analysis.entries
    print(f"\n空间浏览：{analysis.root}", file=output)
    if analysis.volume_total is not None and analysis.volume_free is not None:
        print(
            f"卷空间：{human(analysis.volume_free)} 可用 / "
            f"{human(analysis.volume_total)} 总计",
            file=output,
        )
    print("─" * 88, file=output)
    for index, entry in enumerate(entries, start=1):
        marker = "→" if _is_directory(entry.item.path) else " "
        cloud = (
            f"  云占位 {entry.item.cloud_file_count}"
            if entry.item.cloud_file_count
            else ""
        )
        print(
            f"{index:>3}. {human(entry.item.size):>10}  "
            f"{entry.percent:6.1f}%  {marker} {entry.item.path}{cloud}",
            file=output,
        )
    print("─" * 88, file=output)
    if len(entries) < len(analysis.entries):
        print(
            f"仅显示最大的 {len(entries)}/{len(analysis.entries)} 项；"
            "容量与百分比基于当前层级全部项目。",
            file=output,
        )
    print(
        "输入编号进入目录；`..` 返回；`o N` 在 Finder 中显示；"
        "`r` 刷新；`q` 退出。",
        file=output,
    )
    for issue in analysis.issues:
        location = f" ({issue.path})" if issue.path is not None else ""
        print(
            f"[{issue.code}] {issue.task}: {issue.message}{location}",
            file=error,
        )
    return entries


def run_space_browser(
    start: Path,
    *,
    protection: Predicate,
    top: int = 0,
    input_fn: Callable[[str], str] | None = None,
    output: TextIO | None = None,
    error: TextIO | None = None,
    analyzer: Callable[..., SpaceAnalysis] = analyze_path,
    revealer: Callable[[Path], None] = reveal_in_finder,
) -> int:
    """运行只读行式浏览器；所有写操作均不在该状态机内。"""
    output = output or sys.stdout
    error = error or sys.stderr
    read = input_fn or input
    current = start.expanduser()
    history: list[Path] = []
    control = Control()

    while True:
        try:
            analysis = analyzer(
                current,
                protection=protection,
                control=control,
            )
        except AnalyzeError as exc:
            print(str(exc), file=error)
            if history:
                current = history.pop()
                continue
            return 2
        except KeyboardInterrupt:
            control.cancel()
            print("\n已取消。", file=error)
            return 130

        entries = _print_analysis(
            analysis,
            top=top,
            output=output,
            error=error,
        )
        try:
            command = read("analyze> ").strip()
        except EOFError:
            return 0
        except KeyboardInterrupt:
            print(file=output)
            return 130
        lowered = command.lower()
        if lowered in {"q", "quit", "exit"}:
            return 0
        if lowered in {"", "r", "refresh"}:
            continue
        if lowered in {"..", "up", "back"}:
            if history:
                current = history.pop()
            else:
                print("已经位于起始目录。", file=error)
            continue

        reveal = False
        index_text = command
        if lowered.startswith("o "):
            reveal = True
            index_text = command[2:].strip()
        elif lowered.startswith("open "):
            reveal = True
            index_text = command[5:].strip()
        try:
            index = int(index_text)
        except ValueError:
            print("无法识别命令；输入 `q` 退出。", file=error)
            continue
        if index < 1 or index > len(entries):
            print(f"编号超出范围：{index}", file=error)
            continue

        selected = entries[index - 1].item.path
        if reveal:
            try:
                revealer(selected)
            except RevealError as exc:
                print(f"Finder reveal 失败：{exc}", file=error)
            else:
                print(f"已在 Finder 中显示：{selected}", file=output)
            continue
        if not _is_directory(selected):
            print(f"不是可进入的目录：{selected}", file=error)
            continue
        history.append(current)
        current = selected
