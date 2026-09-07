"""Run/Finding 标识生成（AR-04 §3）。

标识稳定、唯一、跨命令可读，且**不编码路径**：从 ID 本身不能反推目标路径。
只用标准库 ``secrets``，不引入 ulid 等第三方依赖。
"""
from __future__ import annotations

import secrets

ID_PREFIX_FINDING = "finding:"
ID_PREFIX_RUN = "run:"

_TOKEN_BYTES = 12  # 24 个十六进制字符，足够单机短生命周期内唯一


def _token() -> str:
    return secrets.token_hex(_TOKEN_BYTES)


def new_run_id() -> str:
    return f"{ID_PREFIX_RUN}{_token()}"


def new_finding_id() -> str:
    return f"{ID_PREFIX_FINDING}{_token()}"


def valid_id(value: object, prefix: str) -> bool:
    """Opaque identifiers are single, bounded filename-safe tokens, never paths."""
    import re

    if not isinstance(value, str) or not value.startswith(prefix):
        return False
    token = value[len(prefix):]
    return token != "redacted" and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", token) is not None
