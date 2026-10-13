"""CLI 偏好设置的零依赖 JSON 存储。"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from .models import normalize_path
from .predicates import ProtectionGate
from .resource_walk import open_directory

CONFIG_SCHEMA_VERSION = 1
DEFAULT_CONFIG_PATH = Path("~/.config/openclean/config.json")
MAX_FILE_ROOTS = 64


class ConfigError(ValueError):
    pass


def normalized_file_roots(values: list[str | os.PathLike[str]]) -> tuple[str, ...]:
    """只规范化和去重明确给出的根，不跟随链接、不合并父子目录范围。"""
    if len(values) > MAX_FILE_ROOTS:
        raise ConfigError(f"文件审阅根最多 {MAX_FILE_ROOTS} 个")
    roots: list[str] = []
    for value in values:
        raw = os.fspath(value)
        if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
            raise ConfigError("文件审阅根必须是非空目录路径")
        root = str(normalize_path(raw))
        if root not in roots:
            roots.append(root)
    return tuple(roots)


def validated_file_roots(values: list[str | os.PathLike[str]]) -> tuple[str, ...]:
    roots = normalized_file_roots(values)
    for root in roots:
        try:
            # 复用逐组件 no-follow 检查，保存配置不枚举目录内容。
            with open_directory(Path(root), ProtectionGate()):
                pass
        except OSError as exc:
            raise ConfigError(f"文件审阅根不可用（需存在、非链接、非云占位目录）：{root}：{exc}") from exc
    return roots


@dataclass(frozen=True)
class CliConfig:
    analytics_enabled: bool = False
    file_roots: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value: object) -> CliConfig:
        if not isinstance(value, dict):
            raise ConfigError("配置根对象必须是 JSON 对象")
        unknown = sorted(set(value) - {"schema_version", "analytics_enabled", "file_roots"})
        if unknown:
            raise ConfigError(f"配置包含未知字段：{', '.join(unknown)}")
        version = value.get("schema_version", CONFIG_SCHEMA_VERSION)
        if type(version) is not int or version != CONFIG_SCHEMA_VERSION:
            raise ConfigError(
                f"不支持的配置 schema_version：{version!r}"
            )
        analytics = value.get("analytics_enabled", False)
        if type(analytics) is not bool:
            raise ConfigError("analytics_enabled 必须是布尔值")
        file_roots = value.get("file_roots", [])
        if not isinstance(file_roots, list) or any(
            not isinstance(root, str) or not os.path.isabs(root) or "\x00" in root
            for root in file_roots
        ):
            raise ConfigError("file_roots 必须是绝对目录路径的数组")
        # 读取不验证存在性：离线卷保留原配置，由扫描报告未读范围。
        return cls(analytics_enabled=analytics, file_roots=normalized_file_roots(file_roots))

    def to_mapping(self) -> dict[str, object]:
        result = {
            "schema_version": CONFIG_SCHEMA_VERSION,
            "analytics_enabled": self.analytics_enabled,
        }
        # 清空后恢复旧配置形状，未设置新偏好时不强制改写现有文件。
        if self.file_roots:
            result["file_roots"] = list(self.file_roots)
        return result


class ConfigStore:
    """读取和原子更新 CLI 配置，文件权限固定为 ``0600``。"""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = normalize_path(path or DEFAULT_CONFIG_PATH)

    def load(self) -> CliConfig:
        if not self.path.exists():
            return CliConfig()
        try:
            raw = self.path.read_text(encoding="utf-8")
        except UnicodeError as exc:
            raise ConfigError(f"配置不是有效 UTF-8：{self.path}") from exc
        except OSError as exc:
            raise ConfigError(f"无法读取配置 {self.path}：{exc}") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"配置不是有效 JSON：{self.path}:{exc.lineno}:{exc.colno}: "
                f"{exc.msg}"
            ) from exc
        except RecursionError as exc:
            raise ConfigError(f"配置 JSON 嵌套过深：{self.path}") from exc
        return CliConfig.from_mapping(payload)

    def set_analytics(self, enabled: bool) -> CliConfig:
        current = self.load()
        updated = replace(current, analytics_enabled=enabled)
        if current == updated and self.path.exists():
            return current
        self._write(updated)
        return updated

    def set_file_roots(self, values: list[str | os.PathLike[str]]) -> CliConfig:
        current = self.load()
        updated = replace(current, file_roots=validated_file_roots(values))
        if current == updated:
            return current
        self._write(updated)
        return updated

    def _write(self, config: CliConfig) -> None:
        parent = self.path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=parent,
            )
        except OSError as exc:
            raise ConfigError(f"无法准备配置写入：{exc}") from exc

        temporary = Path(temporary_name)
        descriptor_open = True
        try:
            os.fchmod(descriptor, 0o600)
            stream = os.fdopen(descriptor, "w", encoding="utf-8")
            descriptor_open = False
            with stream:
                json.dump(
                    config.to_mapping(),
                    stream,
                    ensure_ascii=False,
                    indent=2,
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        except OSError as exc:
            raise ConfigError(f"无法写入配置 {self.path}：{exc}") from exc
        finally:
            if descriptor_open:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass
