"""三种调用方式的集中模式路由：显式参数优先于环境探测。

人工文本 CLI、程序化 CLI（``--json``）和人工 TUI 共用同一功能内核；
本模块只判定显示与交互模式，不改变任何执行资格、选择语义或退出码。
契约矩阵见 docs/MODES.md；TTY 表示终端能力，不推断调用者是人还是程序。
"""
from __future__ import annotations

TEXT_MODE = "text"
LINE_MODE = "line"
TUI_MODE = "tui"


class DisplayModeError(ValueError):
    """模式冲突或显式交互缺少终端能力；必须在扫描前报错，不静默取舍。"""

    def __init__(self, message: str, *, code: str = "invalid_mode_options") -> None:
        super().__init__(message)
        self.code = code


def resolve_display_mode(
    *,
    json_output: bool,
    stdin_isatty: bool,
    stdout_isatty: bool,
    interactive: bool = False,
    line_interactive: bool = False,
    no_interactive: bool = False,
    parameterized_selection: bool = False,
) -> str:
    """判定一次调用的显示模式，返回 ``text`` / ``line`` / ``tui``。

    显式模式（``--interactive`` / ``--line-interactive`` / ``--no-interactive``）
    互斥且优先于 TTY 探测；``--json`` 搭配 ``--no-interactive`` 是冗余的明确意图，
    继续允许。无显式模式时沿用兼容期自动判断；“显式子命令默认文本 CLI”
    的默认行为迁移单独决策，不在本函数内改变。
    """
    explicit = [
        flag
        for flag, enabled in (
            ("--interactive", interactive),
            ("--line-interactive", line_interactive),
            ("--no-interactive", no_interactive),
        )
        if enabled
    ]
    if len(explicit) > 1:
        raise DisplayModeError(
            f"{' 与 '.join(explicit)} 互斥；一次调用只能显式指定一种模式。"
        )
    if json_output and (interactive or line_interactive):
        raise DisplayModeError(
            "--json 是一次性的机器输出，不能与 --interactive 或 "
            "--line-interactive 同时使用。"
        )
    if interactive:
        if parameterized_selection:
            raise DisplayModeError(
                "--interactive 由菜单构造选择，不能与参数化选择"
                "（--select/--all/--include-confirm/--include-critical/--force）"
                "同时使用。"
            )
        if not (stdin_isatty and stdout_isatty):
            raise DisplayModeError(
                "--interactive 需要 stdin 与 stdout 同时连接终端；"
                "程序化调用请使用 --json 或 --no-interactive。",
                code="interactive_requires_terminal",
            )
        return TUI_MODE
    if line_interactive:
        return LINE_MODE
    if no_interactive or json_output:
        return TEXT_MODE
    if stdin_isatty and stdout_isatty and not parameterized_selection:
        return TUI_MODE
    return TEXT_MODE
