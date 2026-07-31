"""Docker daemon 磁盘占用扫描。

只调用 Docker 官方 CLI 的只读 ``docker system df --format json``，不读取或
猜测 Docker Desktop 的内部存储路径。返回值中的 ``Item.size`` 表示 daemon
报告的可回收估算，而不是资源总占用。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .models import Item, ScanIssue, ScanResult

DOCKER_SCAN_TASK = "docker-system-df"
DEFAULT_DOCKER_TIMEOUT = 20.0
DEFAULT_DOCKER_PRUNE_TIMEOUT = 60.0


class DockerPruneError(RuntimeError):
    pass


@dataclass(frozen=True)
class DockerPruneResult:
    reclaimed_bytes: int
    message: str


@dataclass(frozen=True)
class _ResourcePolicy:
    identifier: str
    category: str
    safety: str
    note: str
    prune_command: tuple[str, ...] | None


_RESOURCE_POLICIES: dict[str, _ResourcePolicy] = {
    "Build Cache": _ResourcePolicy(
        "docker:build-cache",
        "Docker 构建缓存",
        "safe",
        "构建缓存可重新生成",
        ("builder", "prune", "--all", "--force"),
    ),
    "Images": _ResourcePolicy(
        "docker:images",
        "Docker 镜像",
        "confirm",
        "移除后可能需要重新拉取镜像",
        ("image", "prune", "--all", "--force"),
    ),
    "Containers": _ResourcePolicy(
        "docker:containers",
        "Docker 容器",
        "confirm",
        "仅报告 daemon 判定可回收的容器占用",
        ("container", "prune", "--force"),
    ),
    "Local Volumes": _ResourcePolicy(
        "docker:local-volumes",
        "Docker 本地卷",
        "critical",
        "卷可能包含不可重建数据，永不默认选择",
        None,
    ),
}

_SIZE_PATTERN = re.compile(
    r"^([0-9]+(?:\.[0-9]+)?)\s*([kmgtpe]?i?b)$",
    re.IGNORECASE,
)
_DECIMAL_FACTORS = {
    "b": 1,
    "kb": 1000,
    "mb": 1000**2,
    "gb": 1000**3,
    "tb": 1000**4,
    "pb": 1000**5,
    "eb": 1000**6,
}
_BINARY_FACTORS = {
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
    "tib": 1024**4,
    "pib": 1024**5,
    "eib": 1024**6,
}


def parse_docker_size(value: object) -> int:
    """解析 Docker CLI 的人类可读容量，包括 reclaimable 后的百分比。"""
    if not isinstance(value, str):
        raise TypeError("容量字段必须是字符串")
    size_text = value.split("(", 1)[0].strip()
    match = _SIZE_PATTERN.fullmatch(size_text)
    if match is None:
        raise ValueError(f"无法识别容量：{value!r}")
    try:
        number = Decimal(match.group(1))
    except InvalidOperation as exc:
        raise ValueError(f"无法识别容量：{value!r}") from exc
    unit = match.group(2).lower()
    factor = _BINARY_FACTORS.get(unit, _DECIMAL_FACTORS.get(unit))
    if factor is None:
        raise ValueError(f"无法识别容量单位：{value!r}")
    return int(number * factor)


def _parse_count(value: object, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 不是整数：{value!r}") from exc
    if parsed < 0:
        raise ValueError(f"{field} 不能为负数：{value!r}")
    return parsed


def _bounded_message(value: object, fallback: str) -> str:
    if isinstance(value, bytes):
        message = value.decode("utf-8", errors="replace")
    else:
        message = str(value or "")
    message = " ".join(message.strip().split())
    return (message or fallback)[:500]


def _invalid_output_issue(message: str) -> ScanIssue:
    return ScanIssue(
        code="tool_output_invalid",
        message=message,
        task=DOCKER_SCAN_TASK,
    )


def _item_from_row(row: object) -> Item | None:
    if not isinstance(row, dict):
        raise TypeError("JSON 行必须是对象")
    resource_type = row.get("Type")
    if not isinstance(resource_type, str) or not resource_type.strip():
        raise ValueError("缺少 Type")
    resource_type = resource_type.strip()
    policy = _RESOURCE_POLICIES.get(resource_type)
    if policy is None:
        normalized = re.sub(
            r"[^a-z0-9]+", "-", resource_type.lower()
        ).strip("-")
        policy = _ResourcePolicy(
            f"docker:{normalized or 'unknown'}",
            f"Docker {resource_type}",
            "critical",
            "未知 Docker 资源类型，永不默认选择",
            None,
        )

    total_count = _parse_count(row.get("TotalCount"), "TotalCount")
    active_count = _parse_count(row.get("Active"), "Active")
    total_size = parse_docker_size(row.get("Size"))
    reclaimable_size = parse_docker_size(row.get("Reclaimable"))
    if reclaimable_size <= 0:
        return None

    note = (
        f"{policy.note}；共 {total_count} 项，活跃 {active_count} 项；"
        "容量来自 docker system df 的可回收估算"
    )
    return Item(
        path=None,
        size=reclaimable_size,
        category=policy.category,
        safety=policy.safety,
        note=note,
        preselected=False,
        domain="developer",
        requires_explicit_selection=policy.prune_command is not None,
        resource_kind="docker",
        identifier=policy.identifier,
        resource_total_size=total_size,
        total_count=total_count,
        active_count=active_count,
        actionable=policy.prune_command is not None,
        action_block_reason=(
            "Docker 本地卷或未知资源不支持自动清理"
            if policy.prune_command is None
            else ""
        ),
    )


def _policy_for_identifier(identifier: str) -> _ResourcePolicy | None:
    return next(
        (
            policy
            for policy in _RESOURCE_POLICIES.values()
            if policy.identifier == identifier
        ),
        None,
    )


def docker_prune_supported(identifier: str) -> bool:
    policy = _policy_for_identifier(identifier)
    return policy is not None and policy.prune_command is not None


_RECLAIMED_PATTERN = re.compile(
    r"^Total reclaimed space:\s*(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def prune_docker_resource(
    identifier: str,
    *,
    docker_path: str | None = None,
    timeout: float = DEFAULT_DOCKER_PRUNE_TIMEOUT,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    finder: Callable[[str], str | None] | None = None,
) -> DockerPruneResult:
    """按已审计映射执行一个 Docker prune，不接受任意子命令。"""
    policy = _policy_for_identifier(identifier)
    if policy is None or policy.prune_command is None:
        raise DockerPruneError(f"不支持自动清理 Docker 资源：{identifier}")

    find_binary = finder or shutil.which
    binary = docker_path if docker_path is not None else find_binary("docker")
    if binary is None:
        raise DockerPruneError("未找到 Docker CLI")
    command = [binary, *policy.prune_command]
    run = runner or subprocess.run
    try:
        completed = run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise DockerPruneError(
            _bounded_message(
                exc.stderr,
                f"Docker prune 在 {timeout:g} 秒内未完成",
            )
        ) from exc
    except OSError as exc:
        raise DockerPruneError(
            _bounded_message(exc, "无法启动 Docker CLI")
        ) from exc
    if completed.returncode != 0:
        raise DockerPruneError(
            _bounded_message(
                completed.stderr,
                f"Docker prune 退出码 {completed.returncode}",
            )
        )

    match = _RECLAIMED_PATTERN.search(completed.stdout)
    if match is None:
        return DockerPruneResult(
            reclaimed_bytes=0,
            message="Docker prune 已完成，但输出未提供可解析的释放容量",
        )
    try:
        reclaimed = parse_docker_size(match.group(1))
    except (TypeError, ValueError) as exc:
        return DockerPruneResult(
            reclaimed_bytes=0,
            message=(
                "Docker 官方 prune 已完成，但释放容量输出无法解析："
                f"{_bounded_message(exc, '未知格式')}"
            ),
        )
    return DockerPruneResult(
        reclaimed_bytes=reclaimed,
        message="Docker 官方 prune 已完成",
    )


def scan_docker_resources(
    *,
    docker_path: str | None = None,
    timeout: float = DEFAULT_DOCKER_TIMEOUT,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    finder: Callable[[str], str | None] | None = None,
) -> ScanResult:
    """读取 Docker daemon 的资源汇总；未安装 Docker 时静默跳过。"""
    result = ScanResult()
    find_binary = finder or shutil.which
    binary = docker_path if docker_path is not None else find_binary("docker")
    if binary is None:
        return result

    run = runner or subprocess.run
    command = [binary, "system", "df", "--format", "json"]
    try:
        completed = run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        result.issues.append(
            ScanIssue(
                code="tool_unavailable",
                message=_bounded_message(
                    exc.stderr,
                    f"Docker daemon 在 {timeout:g} 秒内未响应",
                ),
                task=DOCKER_SCAN_TASK,
            )
        )
        return result
    except OSError as exc:
        result.issues.append(
            ScanIssue(
                code="tool_unavailable",
                message=_bounded_message(exc, "无法启动 Docker CLI"),
                task=DOCKER_SCAN_TASK,
            )
        )
        return result

    if completed.returncode != 0:
        result.issues.append(
            ScanIssue(
                code="tool_unavailable",
                message=_bounded_message(
                    completed.stderr,
                    f"Docker CLI 退出码 {completed.returncode}",
                ),
                task=DOCKER_SCAN_TASK,
            )
        )
        return result

    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        result.issues.append(_invalid_output_issue("Docker CLI 返回了空输出"))
        return result

    for line_number, line in enumerate(lines, start=1):
        try:
            row = json.loads(line)
            item = _item_from_row(row)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            result.issues.append(
                _invalid_output_issue(f"第 {line_number} 行：{exc}")
            )
            continue
        if item is not None:
            result.items.append(item)
    return result
