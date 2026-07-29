"""扫描领域模型。

模型只描述扫描事实和结果，不负责路径发现、策略判定或文件操作。
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

SAFETY_LEVELS = frozenset({"safe", "confirm", "critical"})
RESOURCE_KINDS = frozenset({"filesystem", "docker"})
CLEANUP_SCOPES = frozenset({"", "darwin-user-cache"})
PATH_SOURCES = frozenset({"builtin", "environment"})


def normalize_path(path: str | os.PathLike[str]) -> Path:
    """展开用户目录并做词法规范化，但不解析符号链接。"""
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


@dataclass(frozen=True)
class FileIdentity:
    """用于在扫描与后续操作之间识别同一个文件系统对象。"""

    device: int
    inode: int
    owner: int

    @classmethod
    def from_stat(cls, stat_result: os.stat_result) -> FileIdentity:
        return cls(
            device=stat_result.st_dev,
            inode=stat_result.st_ino,
            owner=stat_result.st_uid,
        )


@dataclass(frozen=True)
class FileFacts:
    """谓词判定所需的不可变文件事实。"""

    path: Path
    stat: os.stat_result | None

    @classmethod
    def from_path(cls, path: str | os.PathLike[str]) -> FileFacts:
        normalized = normalize_path(path)
        try:
            stat_result = normalized.lstat()
        except (FileNotFoundError, PermissionError, OSError):
            stat_result = None
        return cls(path=normalized, stat=stat_result)

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def file_url(self) -> str:
        return self.path.as_uri()

    @property
    def identity(self) -> FileIdentity | None:
        if self.stat is None:
            return None
        return FileIdentity.from_stat(self.stat)

    @property
    def logical_size(self) -> int:
        return self.stat.st_size if self.stat is not None else 0

    @property
    def allocated_size(self) -> int:
        if self.stat is None:
            return 0
        blocks = getattr(self.stat, "st_blocks", None)
        return blocks * 512 if blocks is not None else self.stat.st_size

    @property
    def is_probable_cloud_placeholder(self) -> bool:
        if self.stat is None:
            return False
        mode = getattr(self.stat, "st_mode", None)
        if mode is not None and not stat.S_ISREG(mode):
            return False
        blocks = getattr(self.stat, "st_blocks", None)
        return blocks == 0 and self.stat.st_size > 0


@dataclass(frozen=True)
class Item:
    """一个已通过扫描策略的候选可清理项。"""

    path: Path | None
    size: int
    category: str
    safety: str = "safe"
    note: str = ""
    logical_size: int | None = None
    allocated_size: int | None = None
    is_cloud_file: bool = False
    cloud_file_count: int = 0
    cloud_logical_size: int = 0
    actionable: bool = True
    action_block_reason: str = ""
    requires_privilege: bool = False
    identity: FileIdentity | None = None
    project_root: Path | None = None
    artifact_name: str = ""
    latest_mtime: float | None = None
    age_days: int | None = None
    preselected: bool | None = None
    excluded_paths: int = 0
    domain: str = ""
    path_source: str = "builtin"
    requires_explicit_selection: bool = False
    resource_kind: str = "filesystem"
    identifier: str = ""
    resource_total_size: int | None = None
    total_count: int | None = None
    active_count: int | None = None
    running_process_markers: tuple[str, ...] = ()
    cleanup_scope: str = ""
    cleanup_root: Path | None = None
    cleanup_root_identity: FileIdentity | None = None
    startup_program: str = ""
    startup_program_uses_path: bool = False

    def __post_init__(self) -> None:
        if self.safety not in SAFETY_LEVELS:
            raise ValueError(f"未知安全等级：{self.safety}")
        if self.resource_kind not in RESOURCE_KINDS:
            raise ValueError(f"未知资源类型：{self.resource_kind}")
        if self.path_source not in PATH_SOURCES:
            raise ValueError(f"未知路径来源：{self.path_source}")
        if self.cleanup_scope not in CLEANUP_SCOPES:
            raise ValueError(f"未知 cleanup_scope：{self.cleanup_scope}")
        if self.resource_kind == "filesystem" and self.path is None:
            raise ValueError("文件系统资源必须包含 path")
        if self.resource_kind != "filesystem" and not self.identifier:
            raise ValueError("非文件系统资源必须包含 identifier")
        if self.size < 0:
            raise ValueError("size 不能为负数")
        if self.excluded_paths < 0:
            raise ValueError("excluded_paths 不能为负数")
        if self.cloud_file_count < 0:
            raise ValueError("cloud_file_count 不能为负数")
        if self.cloud_logical_size < 0:
            raise ValueError("cloud_logical_size 不能为负数")
        if self.resource_total_size is not None and self.resource_total_size < 0:
            raise ValueError("resource_total_size 不能为负数")
        if self.total_count is not None and self.total_count < 0:
            raise ValueError("total_count 不能为负数")
        if self.active_count is not None and self.active_count < 0:
            raise ValueError("active_count 不能为负数")
        if any(not marker.strip() for marker in self.running_process_markers):
            raise ValueError("running_process_markers 不能包含空字符串")
        if self.cleanup_scope and (
            self.cleanup_root is None or self.cleanup_root_identity is None
        ):
            raise ValueError("特殊清理范围必须包含根路径及其 inode 身份")
        if not self.cleanup_scope and (
            self.cleanup_root is not None or self.cleanup_root_identity is not None
        ):
            raise ValueError("普通候选不能携带特殊清理根")
        if self.startup_program_uses_path and not self.startup_program:
            raise ValueError("PATH 启动程序引用不能为空")
        if self.startup_program and self.resource_kind != "filesystem":
            raise ValueError("启动项程序引用只能用于文件系统资源")


@dataclass(frozen=True)
class ScanIssue:
    """不会阻断整体扫描、但会降低结果完整性的结构化问题。"""

    code: str
    message: str
    task: str = ""
    path: Path | None = None
    blocking: bool = True


@dataclass
class ScanResult:
    items: list[Item] = field(default_factory=list)
    issues: list[ScanIssue] = field(default_factory=list)
    cancelled: bool = False

    @property
    def total(self) -> int:
        return sum(item.size for item in self.items)

    @property
    def actionable_total(self) -> int:
        return sum(item.size for item in self.items if item.actionable)

    @property
    def requires_privilege_total(self) -> int:
        return sum(item.size for item in self.items if item.requires_privilege)

    @property
    def unsupported_total(self) -> int:
        return sum(
            item.size
            for item in self.items
            if not item.actionable and not item.requires_privilege
        )

    @property
    def complete(self) -> bool:
        return not self.cancelled and not any(
            issue.blocking for issue in self.issues
        )

    def by_category(self) -> dict[str, list[Item]]:
        grouped: dict[str, list[Item]] = {}
        for item in self.items:
            grouped.setdefault(item.category, []).append(item)
        return grouped

    def by_project(self) -> dict[Path, list[Item]]:
        grouped: dict[Path, list[Item]] = {}
        for item in self.items:
            if item.project_root is not None:
                grouped.setdefault(item.project_root, []).append(item)
        return grouped

    def by_domain(self) -> dict[str, list[Item]]:
        grouped: dict[str, list[Item]] = {}
        for item in self.items:
            grouped.setdefault(item.domain, []).append(item)
        return grouped

    @property
    def preselected_total(self) -> int:
        return sum(item.size for item in self.items if item.preselected is True)
