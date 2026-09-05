"""Run/Finding 标识生成（AR-04 §3）。

标识稳定、唯一、跨命令可读，且**不编码路径**：从 ID 本身不能反推目标路径。
只用标准库 ``secrets``，不引入 ulid 等第三方依赖。
"""
from __future__ import annotations

import secrets

from .models import ID_PREFIX_FINDING, ID_PREFIX_RUN

_TOKEN_BYTES = 12  # 24 个十六进制字符，足够单机短生命周期内唯一


def _token() -> str:
    return secrets.token_hex(_TOKEN_BYTES)


def new_run_id() -> str:
    return f"{ID_PREFIX_RUN}{_token()}"


def new_finding_id() -> str:
    return f"{ID_PREFIX_FINDING}{_token()}"
