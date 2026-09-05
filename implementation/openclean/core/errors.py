"""Agent Runtime 错误类型与退出码映射（AR-03 §8）。

退出码语义沿用既有 CLI：``1`` = blocking/失败/能力 unavailable；``2`` = 参数/规则/路径/
选择/配置错误。每个异常携带 ``exit_code``，CLI 层据此返回，避免各处硬编码。
"""
from __future__ import annotations


class AgentRuntimeError(Exception):
    """Agent Runtime 层错误基类。"""

    exit_code = 2

    def __init__(self, message: str, *, code: str = "agent_runtime_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class RunStoreError(AgentRuntimeError):
    """Run Store 读写、权限或状态错误（fail-closed）。"""

    exit_code = 1

    def __init__(self, message: str, *, code: str = "run_store_error") -> None:
        super().__init__(message, code=code)


class RunNotFoundError(RunStoreError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="run_not_found")


class RunExpiredError(RunStoreError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="run_expired")


class FindingNotInRunError(AgentRuntimeError):
    """``--finding`` 不属于 ``--run``（AR-03 §4）。"""

    exit_code = 2

    def __init__(self, message: str) -> None:
        super().__init__(message, code="finding_not_in_run")


class StrategyError(AgentRuntimeError):
    """策略加载/校验错误。"""

    exit_code = 2

    def __init__(self, message: str, *, code: str = "strategy_error") -> None:
        super().__init__(message, code=code)


class PackNotFoundError(StrategyError):
    """请求的 pack 未随包分发或不存在（能力 unavailable）。"""

    exit_code = 1

    def __init__(self, message: str) -> None:
        super().__init__(message, code="pack_not_found")


class TargetUnavailableError(AgentRuntimeError):
    """inspect TARGET 尚未实现（不伪装完成，AGENTS.md 硬约束 6）。"""

    exit_code = 1

    def __init__(self, message: str) -> None:
        super().__init__(message, code="target_unavailable")


class PlanError(AgentRuntimeError, ValueError):
    """An execution plan is incomplete, inconsistent, or lacks fresh consent."""

    exit_code = 1

    def __init__(self, message: str, *, code: str = "invalid_plan") -> None:
        super().__init__(message, code=code)
