"""CLI 偏好设置的零依赖 JSON 存储。"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .models import normalize_path

CONFIG_SCHEMA_VERSION = 1
DEFAULT_CONFIG_PATH = Path("~/.config/openclean/config.json").expanduser()


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class CliConfig:
    analytics_enabled: bool = False

    @classmethod
    def from_mapping(cls, value: object) -> CliConfig:
        if not isinstance(value, dict):
            raise ConfigError("配置根对象必须是 JSON 对象")
        unknown = sorted(set(value) - {"schema_version", "analytics_enabled"})
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
        return cls(analytics_enabled=analytics)

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": CONFIG_SCHEMA_VERSION,
            "analytics_enabled": self.analytics_enabled,
        }


class ConfigStore:
    """读取和原子更新 CLI 配置，文件权限固定为 ``0600``。"""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = normalize_path(path or DEFAULT_CONFIG_PATH)

    def load(self) -> CliConfig:
        if not self.path.exists():
            return CliConfig()
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"无法读取配置 {self.path}：{exc}") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"配置不是有效 JSON：{self.path}:{exc.lineno}:{exc.colno}: "
                f"{exc.msg}"
            ) from exc
        return CliConfig.from_mapping(payload)

    def set_analytics(self, enabled: bool) -> CliConfig:
        current = self.load()
        updated = CliConfig(analytics_enabled=enabled)
        if current == updated and self.path.exists():
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
