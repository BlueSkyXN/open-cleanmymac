"""Docker daemon 磁盘占用扫描。

固定 Docker CLI 的 canonical realpath，再通过只读 context inspect、daemon info 和
``docker system df --format json`` 绑定扫描目标，不读取或猜测 Docker Desktop 的内部
存储路径。返回值中的 ``Item.size`` 表示 daemon 报告的可回收估算，而不是资源总占用。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation

from .models import Item, ScanIssue, ScanResult

DOCKER_SCAN_TASK = "docker-system-df"
DOCKER_TARGET_TASK = "docker-target-identity"
DEFAULT_DOCKER_TIMEOUT = 20.0
DEFAULT_DOCKER_PRUNE_TIMEOUT = 60.0
DOCKER_BINDING_VERSION = 2


class DockerTargetError(RuntimeError):
    pass


class DockerPruneError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        side_effect_unknown: bool = False,
    ) -> None:
        super().__init__(message)
        self.side_effect_unknown = side_effect_unknown


@dataclass(frozen=True)
class DockerPruneResult:
    reclaimed_bytes: int
    message: str


@dataclass(frozen=True)
class DockerTargetIdentity:
    cli_path: str
    context_name: str
    target_kind: str
    target_value: str
    endpoint_host: str
    skip_tls_verify: bool
    daemon_id: str

    def __post_init__(self) -> None:
        _validated_cli_path(self.cli_path)
        _validated_target_text(self.context_name, "context_name", 256)
        _validated_target_text(self.target_value, "target.value", 2048)
        _validated_target_text(self.endpoint_host, "endpoint_host", 2048)
        _validated_target_text(self.daemon_id, "daemon_id", 512)
        if (
            not isinstance(self.target_kind, str)
            or self.target_kind not in {"context", "host"}
        ):
            raise DockerTargetError("Docker binding target.kind 无效")
        if type(self.skip_tls_verify) is not bool:
            raise DockerTargetError("Docker binding skip_tls_verify 无效")
        if self.target_kind == "host":
            if self.context_name != "default":
                raise DockerTargetError("Docker host binding 必须使用 default context")
            if self.target_value != self.endpoint_host:
                raise DockerTargetError("Docker host binding 与 endpoint 不一致")
        elif self.target_value != self.context_name:
            raise DockerTargetError("Docker context binding 与 context_name 不一致")

    @property
    def command_prefix(self) -> tuple[str, str]:
        option = "--context" if self.target_kind == "context" else "--host"
        return option, self.target_value


def _validated_target_text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise DockerTargetError(f"Docker binding {field} 无效")
    if len(value) > maximum or any(ord(char) < 32 for char in value):
        raise DockerTargetError(f"Docker binding {field} 无效")
    return value


def _validated_cli_path(value: object) -> str:
    path = _validated_target_text(value, "cli_path", 4096)
    if (
        not os.path.isabs(path)
        or os.path.normpath(path) != path
        or os.path.realpath(path) != path
    ):
        raise DockerTargetError("Docker binding cli_path 无效")
    return path


def _resolved_cli_path(value: object) -> str:
    path = _validated_target_text(value, "cli_path", 4096)
    return _validated_cli_path(os.path.realpath(os.path.abspath(path)))


def encode_docker_resource_binding(target: DockerTargetIdentity) -> str:
    return json.dumps(
        {
            "v": DOCKER_BINDING_VERSION,
            "kind": "docker",
            "cli_path": target.cli_path,
            "context_name": target.context_name,
            "target": {
                "kind": target.target_kind,
                "value": target.target_value,
            },
            "endpoint_host": target.endpoint_host,
            "skip_tls_verify": target.skip_tls_verify,
            "daemon_id": target.daemon_id,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def parse_docker_resource_binding(binding: str) -> DockerTargetIdentity:
    if not binding:
        raise DockerTargetError("Docker 候选缺少扫描时 resource binding")
    try:
        payload = json.loads(binding)
    except (json.JSONDecodeError, TypeError) as exc:
        raise DockerTargetError("Docker 候选 resource binding 无效") from exc
    expected_keys = {
        "v",
        "kind",
        "cli_path",
        "context_name",
        "target",
        "endpoint_host",
        "skip_tls_verify",
        "daemon_id",
    }
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise DockerTargetError("Docker 候选 resource binding 字段无效")
    if type(payload["v"]) is not int or payload["v"] != DOCKER_BINDING_VERSION:
        raise DockerTargetError("Docker 候选 resource binding 版本无效")
    if payload["kind"] != "docker":
        raise DockerTargetError("Docker 候选 resource binding 类型无效")
    target = payload["target"]
    if not isinstance(target, dict) or set(target) != {"kind", "value"}:
        raise DockerTargetError("Docker 候选 resource binding target 无效")
    parsed = DockerTargetIdentity(
        cli_path=payload["cli_path"],
        context_name=payload["context_name"],
        target_kind=target["kind"],
        target_value=target["value"],
        endpoint_host=payload["endpoint_host"],
        skip_tls_verify=payload["skip_tls_verify"],
        daemon_id=payload["daemon_id"],
    )
    if binding != encode_docker_resource_binding(parsed):
        raise DockerTargetError("Docker 候选 resource binding 非规范格式")
    return parsed


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


def _invalid_output_issue(message: str) -> ScanIssue:
    return ScanIssue(
        code="tool_output_invalid",
        message=message,
        task=DOCKER_SCAN_TASK,
    )


def _run_target_probe_command(
    command: list[str],
    *,
    stage: str,
    timeout: float,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> str:
    try:
        completed = runner(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise DockerTargetError(
            f"Docker target {stage} 在 {timeout:g} 秒内未完成"
        ) from exc
    except OSError as exc:
        raise DockerTargetError(f"无法执行 Docker target {stage}") from exc
    if completed.returncode != 0:
        raise DockerTargetError(
            f"Docker target {stage} 失败，退出码 {completed.returncode}"
        )
    return completed.stdout


def _current_context_name(
    binary: str,
    *,
    timeout: float,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> str:
    stdout = _run_target_probe_command(
        [binary, "context", "show"],
        stage="context show",
        timeout=timeout,
        runner=runner,
    )
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise DockerTargetError("Docker target context show 输出无效")
    return _validated_target_text(lines[0], "context_name", 256)


def _inspect_context_endpoint(
    binary: str,
    context_name: str,
    *,
    timeout: float,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> tuple[str, bool]:
    stdout = _run_target_probe_command(
        [binary, "context", "inspect", context_name],
        stage="context inspect",
        timeout=timeout,
        runner=runner,
    )
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise DockerTargetError("Docker target context inspect 输出无效") from exc
    if not isinstance(payload, list) or len(payload) != 1:
        raise DockerTargetError("Docker target context inspect 输出无效")
    context = payload[0]
    if not isinstance(context, dict) or context.get("Name") != context_name:
        raise DockerTargetError("Docker target context identity 无效")
    endpoints = context.get("Endpoints")
    docker_endpoint = (
        endpoints.get("docker") if isinstance(endpoints, dict) else None
    )
    if not isinstance(docker_endpoint, dict):
        raise DockerTargetError("Docker target endpoint 缺失")
    host = _validated_target_text(
        docker_endpoint.get("Host"), "endpoint_host", 2048
    )
    skip_tls_verify = docker_endpoint.get("SkipTLSVerify")
    if type(skip_tls_verify) is not bool:
        raise DockerTargetError("Docker target SkipTLSVerify 无效")
    return host, skip_tls_verify


def probe_docker_target(
    binary: str,
    *,
    timeout: float,
    runner: Callable[..., subprocess.CompletedProcess[str]],
    expected: DockerTargetIdentity | None = None,
) -> DockerTargetIdentity:
    """只读解析有效 Docker target，并取得 Engine ID。"""
    binary = _resolved_cli_path(binary)
    context_name = (
        expected.context_name
        if expected is not None
        else _current_context_name(binary, timeout=timeout, runner=runner)
    )
    endpoint_host, skip_tls_verify = _inspect_context_endpoint(
        binary,
        context_name,
        timeout=timeout,
        runner=runner,
    )
    target_kind = "host" if context_name == "default" else "context"
    target_value = endpoint_host if target_kind == "host" else context_name
    option = "--host" if target_kind == "host" else "--context"
    stdout = _run_target_probe_command(
        [
            binary,
            option,
            target_value,
            "info",
            "--format",
            "{{json .ID}}",
        ],
        stage="daemon info",
        timeout=timeout,
        runner=runner,
    )
    try:
        daemon_id = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise DockerTargetError("Docker target daemon ID 输出无效") from exc
    return DockerTargetIdentity(
        cli_path=binary,
        context_name=context_name,
        target_kind=target_kind,
        target_value=target_value,
        endpoint_host=endpoint_host,
        skip_tls_verify=skip_tls_verify,
        daemon_id=daemon_id,
    )


def _item_from_row(
    row: object,
    *,
    resource_binding: str = "",
) -> Item | None:
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
    prune_supported = policy.prune_command is not None
    binding_verified = bool(resource_binding)
    return Item(
        path=None,
        size=reclaimable_size,
        category=policy.category,
        safety=policy.safety,
        note=note,
        preselected=False,
        domain="developer",
        requires_explicit_selection=prune_supported,
        resource_kind="docker",
        identifier=policy.identifier,
        resource_total_size=total_size,
        total_count=total_count,
        active_count=active_count,
        actionable=prune_supported and binding_verified,
        action_block_reason=(
            "Docker 本地卷或未知资源不支持自动清理"
            if not prune_supported
            else "Docker target 身份无法验证；请重新扫描后再执行"
            if not binding_verified
            else ""
        ),
        resource_binding=resource_binding,
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
    resource_binding: str = "",
    docker_path: str | None = None,
    timeout: float = DEFAULT_DOCKER_PRUNE_TIMEOUT,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    finder: Callable[[str], str | None] | None = None,
) -> DockerPruneResult:
    """按已审计映射执行一个 Docker prune，不接受任意子命令。"""
    policy = _policy_for_identifier(identifier)
    if policy is None or policy.prune_command is None:
        raise DockerPruneError(f"不支持自动清理 Docker 资源：{identifier}")
    try:
        expected_target = parse_docker_resource_binding(resource_binding)
    except DockerTargetError as exc:
        raise DockerPruneError(str(exc)) from exc

    find_binary = finder or shutil.which
    discovered_binary = (
        docker_path if docker_path is not None else find_binary("docker")
    )
    if discovered_binary is None:
        raise DockerPruneError("未找到 Docker CLI")
    try:
        binary = _resolved_cli_path(discovered_binary)
    except DockerTargetError as exc:
        raise DockerPruneError("Docker CLI 路径无效；请重新扫描") from exc
    if binary != expected_target.cli_path:
        raise DockerPruneError(
            "Docker CLI realpath 已变化；请重新扫描后再执行"
        )
    run = runner or subprocess.run
    try:
        current_target = probe_docker_target(
            binary,
            timeout=timeout,
            runner=run,
            expected=expected_target,
        )
    except DockerTargetError as exc:
        raise DockerPruneError(
            "无法复核 Docker target；请重新扫描后再执行"
        ) from exc
    if current_target != expected_target:
        raise DockerPruneError(
            "Docker target 身份已变化；请重新扫描后再执行"
        )
    try:
        binary = _resolved_cli_path(binary)
    except DockerTargetError as exc:
        raise DockerPruneError("Docker CLI 路径无效；请重新扫描") from exc
    if binary != expected_target.cli_path:
        raise DockerPruneError(
            "Docker CLI realpath 已变化；请重新扫描后再执行"
        )
    command = [
        binary,
        *expected_target.command_prefix,
        *policy.prune_command,
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
        raise DockerPruneError(
            f"Docker prune 在 {timeout:g} 秒内未完成",
            side_effect_unknown=True,
        ) from exc
    except OSError as exc:
        raise DockerPruneError("无法启动 Docker CLI") from exc
    if completed.returncode != 0:
        raise DockerPruneError(
            f"Docker prune 失败，退出码 {completed.returncode}",
            side_effect_unknown=True,
        )

    match = _RECLAIMED_PATTERN.search(completed.stdout)
    if match is None:
        return DockerPruneResult(
            reclaimed_bytes=0,
            message="Docker prune 已完成，但输出未提供可解析的释放容量",
        )
    try:
        reclaimed = parse_docker_size(match.group(1))
    except (TypeError, ValueError):
        return DockerPruneResult(
            reclaimed_bytes=0,
            message="Docker 官方 prune 已完成，但释放容量输出无法解析",
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
    discovered_binary = (
        docker_path if docker_path is not None else find_binary("docker")
    )
    if discovered_binary is None:
        return result
    try:
        binary = _resolved_cli_path(discovered_binary)
    except DockerTargetError:
        result.issues.append(
            ScanIssue(
                code="tool_unavailable",
                message="Docker CLI 路径无效",
                task=DOCKER_SCAN_TASK,
            )
        )
        return result

    run = runner or subprocess.run
    target: DockerTargetIdentity | None = None
    target_error: DockerTargetError | None = None
    try:
        target = probe_docker_target(
            binary,
            timeout=timeout,
            runner=run,
        )
    except DockerTargetError as exc:
        target_error = exc
    command = [binary]
    if target is not None:
        command.extend(target.command_prefix)
    command.extend(("system", "df", "--format", "json"))
    try:
        completed = run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        result.issues.append(
            ScanIssue(
                code="tool_unavailable",
                message=f"Docker daemon 在 {timeout:g} 秒内未响应",
                task=DOCKER_SCAN_TASK,
            )
        )
        return result
    except OSError:
        result.issues.append(
            ScanIssue(
                code="tool_unavailable",
                message="无法启动 Docker CLI",
                task=DOCKER_SCAN_TASK,
            )
        )
        return result

    if completed.returncode != 0:
        result.issues.append(
            ScanIssue(
                code="tool_unavailable",
                message=f"Docker CLI 执行失败，退出码 {completed.returncode}",
                task=DOCKER_SCAN_TASK,
            )
        )
        return result

    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        result.issues.append(_invalid_output_issue("Docker CLI 返回了空输出"))
        return result

    resource_binding = (
        encode_docker_resource_binding(target) if target is not None else ""
    )
    for line_number, line in enumerate(lines, start=1):
        try:
            row = json.loads(line)
            item = _item_from_row(row, resource_binding=resource_binding)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            result.issues.append(
                _invalid_output_issue(f"第 {line_number} 行：{exc}")
            )
            continue
        if item is not None:
            result.items.append(item)
    if target_error is not None:
        result.issues.append(
            ScanIssue(
                code="docker_target_unverified",
                message=(
                    "Docker 容量可读取，但 target 身份无法验证；"
                    "相关候选保持不可执行"
                ),
                task=DOCKER_TARGET_TASK,
            )
        )
        return result
    assert target is not None
    try:
        final_target = probe_docker_target(
            binary,
            timeout=timeout,
            runner=run,
            expected=target,
        )
    except DockerTargetError:
        final_target = None
    if final_target != target:
        result.items = [
            replace(
                item,
                actionable=False,
                action_block_reason=(
                    "Docker target 在扫描期间发生变化；请重新扫描"
                    if docker_prune_supported(item.identifier)
                    else item.action_block_reason
                ),
                resource_binding="",
            )
            for item in result.items
        ]
        result.issues.append(
            ScanIssue(
                code="docker_binding_changed",
                message=(
                    "Docker target 在容量读取期间发生变化；"
                    "相关候选保持不可执行"
                ),
                task=DOCKER_TARGET_TASK,
            )
        )
    return result
