"""扫描领域模型。

模型只描述扫描事实和结果，不负责路径发现、策略判定或文件操作。
"""
from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path

SAFETY_LEVELS = frozenset({"safe", "confirm", "critical"})
FILESYSTEM_RESOURCE_KINDS = frozenset({"filesystem", "filesystem_subset"})
RESOURCE_KINDS = frozenset({*FILESYSTEM_RESOURCE_KINDS, "docker"})
CLEANUP_SCOPES = frozenset({"", "darwin-user-cache"})
PATH_SOURCES = frozenset({"builtin", "environment"})
UPDATER_STATUSES = frozenset(
    {
        "",
        "pending_update",
        "same_version_residue",
        "older_version_residue",
        "installed_app_missing",
        "version_unknown",
    }
)
DIAGNOSTIC_KINDS = frozenset(
    {
        "",
        "retention",
        "sqlite_freelist",
        "updater_temp",
        "open_unlinked",
        "codex_transient",
        "crashpad_pairing",
    }
)
FILESYSTEM_SUBSET_DIAGNOSTIC_KINDS = frozenset(
    {"open_unlinked", "codex_transient", "crashpad_pairing"}
)

# ``SF_DATALESS`` is part of the public macOS ``sys/stat.h`` contract.  Older
# Python builds expose ``st_flags`` but not the corresponding ``stat`` constant.
MACOS_SF_DATALESS = (
    getattr(stat, "SF_DATALESS", 0x40000000)
    if sys.platform == "darwin"
    else 0
)


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
    def is_dataless(self) -> bool:
        """Return whether macOS marks this filesystem object as dataless."""
        if self.stat is None or not MACOS_SF_DATALESS:
            return False
        flags = getattr(self.stat, "st_flags", 0)
        return bool(flags & MACOS_SF_DATALESS)

    @property
    def is_probable_cloud_placeholder(self) -> bool:
        if self.stat is None:
            return False
        if self.is_dataless:
            return True
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
    cross_device_paths: int = 0
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
    resource_binding: str = ""
    updater_status: str = ""
    installed_version: str = ""
    staged_version: str = ""
    updater_external_install: bool = False
    diagnostic_kind: str = ""
    open_handle_count: int | None = None
    retention_file_count: int | None = None
    retention_7d_bytes: int | None = None
    retention_14d_bytes: int | None = None
    retention_30d_bytes: int | None = None
    sqlite_page_size: int | None = None
    sqlite_page_count: int | None = None
    sqlite_freelist_count: int | None = None
    sqlite_internal_free_bytes: int | None = None
    sqlite_internal_free_ratio: float | None = None
    sqlite_wal_bytes: int | None = None
    related_process_count: int | None = None
    paired_artifact_count: int | None = None
    recent_artifact_count: int | None = None
    measured_count: int | None = None
    measurement_complete: bool | None = None

    def __post_init__(self) -> None:
        if self.safety not in SAFETY_LEVELS:
            raise ValueError(f"未知安全等级：{self.safety}")
        if self.resource_kind not in RESOURCE_KINDS:
            raise ValueError(f"未知资源类型：{self.resource_kind}")
        if self.path_source not in PATH_SOURCES:
            raise ValueError(f"未知路径来源：{self.path_source}")
        if self.updater_status not in UPDATER_STATUSES:
            raise ValueError(f"未知 updater 状态：{self.updater_status}")
        if self.diagnostic_kind not in DIAGNOSTIC_KINDS:
            raise ValueError(f"未知诊断类型：{self.diagnostic_kind}")
        if self.cleanup_scope not in CLEANUP_SCOPES:
            raise ValueError(f"未知 cleanup_scope：{self.cleanup_scope}")
        if self.resource_kind in FILESYSTEM_RESOURCE_KINDS and self.path is None:
            raise ValueError("文件系统资源必须包含 path")
        if self.resource_kind not in FILESYSTEM_RESOURCE_KINDS and not self.identifier:
            raise ValueError("非文件系统资源必须包含 identifier")
        if self.resource_kind == "filesystem_subset" and (
            self.diagnostic_kind not in FILESYSTEM_SUBSET_DIAGNOSTIC_KINDS
        ):
            raise ValueError("filesystem_subset 只能用于已知只读子集诊断")
        if (
            self.diagnostic_kind in FILESYSTEM_SUBSET_DIAGNOSTIC_KINDS
            and self.resource_kind != "filesystem_subset"
        ):
            raise ValueError("子集诊断必须使用 filesystem_subset 资源类型")
        if self.size < 0:
            raise ValueError("size 不能为负数")
        if self.excluded_paths < 0:
            raise ValueError("excluded_paths 不能为负数")
        if self.cross_device_paths < 0:
            raise ValueError("cross_device_paths 不能为负数")
        if self.cloud_file_count < 0:
            raise ValueError("cloud_file_count 不能为负数")
        if self.cloud_logical_size < 0:
            raise ValueError("cloud_logical_size 不能为负数")
        if self.resource_total_size is not None and self.resource_total_size < 0:
            raise ValueError("resource_total_size 不能为负数")
        if self.total_count is not None and self.total_count < 0:
            raise ValueError("total_count 不能为负数")
        if self.measured_count is not None and self.measured_count < 0:
            raise ValueError("measured_count 不能为负数")
        if self.measured_count is not None and self.total_count is None:
            raise ValueError("measured_count 必须同时包含 total_count")
        if (
            self.measured_count is not None
            and self.total_count is not None
            and self.measured_count > self.total_count
        ):
            raise ValueError("measured_count 不能超过 total_count")
        if self.measurement_complete is not None and self.measured_count is None:
            raise ValueError("measurement_complete 必须同时包含 measured_count")
        if (
            self.measurement_complete is True
            and self.measured_count != self.total_count
        ):
            raise ValueError("完整测量的 measured_count 必须等于 total_count")
        if self.active_count is not None and self.active_count < 0:
            raise ValueError("active_count 不能为负数")
        diagnostic_counts = (
            self.open_handle_count,
            self.retention_file_count,
            self.retention_7d_bytes,
            self.retention_14d_bytes,
            self.retention_30d_bytes,
            self.sqlite_page_size,
            self.sqlite_page_count,
            self.sqlite_freelist_count,
            self.sqlite_internal_free_bytes,
            self.sqlite_wal_bytes,
            self.related_process_count,
            self.paired_artifact_count,
            self.recent_artifact_count,
        )
        if any(value is not None and value < 0 for value in diagnostic_counts):
            raise ValueError("诊断计数和容量不能为负数")
        if (
            self.sqlite_internal_free_ratio is not None
            and not 0.0 <= self.sqlite_internal_free_ratio <= 1.0
        ):
            raise ValueError("SQLite 空闲页比例必须位于 0 到 1")
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
        if self.resource_binding and self.resource_kind in FILESYSTEM_RESOURCE_KINDS:
            raise ValueError("文件系统资源不能携带外部资源 binding")
        if self.updater_status and self.resource_kind != "filesystem":
            raise ValueError("updater 状态只能用于文件系统资源")
        if (
            self.installed_version
            or self.staged_version
            or self.updater_external_install
        ) and not self.updater_status:
            raise ValueError("updater 元数据必须包含 updater_status")
        retention_values = (
            self.retention_file_count,
            self.retention_7d_bytes,
            self.retention_14d_bytes,
            self.retention_30d_bytes,
        )
        if any(value is not None for value in retention_values) and (
            self.diagnostic_kind != "retention"
        ):
            raise ValueError("retention 元数据必须使用 retention 诊断类型")
        if all(value is not None for value in retention_values[1:]) and not (
            self.retention_7d_bytes
            >= self.retention_14d_bytes
            >= self.retention_30d_bytes
        ):
            raise ValueError("retention 容量必须按 7/14/30 天单调递减")
        sqlite_values = (
            self.sqlite_page_size,
            self.sqlite_page_count,
            self.sqlite_freelist_count,
            self.sqlite_internal_free_bytes,
            self.sqlite_internal_free_ratio,
            self.sqlite_wal_bytes,
        )
        if any(value is not None for value in sqlite_values) and (
            self.diagnostic_kind != "sqlite_freelist"
        ):
            raise ValueError("SQLite 元数据必须使用 sqlite_freelist 诊断类型")
        if self.related_process_count is not None and (
            self.diagnostic_kind != "open_unlinked"
        ):
            raise ValueError("关联进程计数必须使用 open_unlinked 诊断类型")
        pairing_values = (
            self.paired_artifact_count,
            self.recent_artifact_count,
        )
        if any(value is not None for value in pairing_values) and (
            self.diagnostic_kind != "crashpad_pairing"
        ):
            raise ValueError("配对计数必须使用 crashpad_pairing 诊断类型")
        if (
            self.recent_artifact_count is not None
            and self.total_count is not None
            and self.recent_artifact_count > self.total_count
        ):
            raise ValueError("最近 artifact 计数不能超过候选总数")
        if (
            self.sqlite_page_count is not None
            and self.sqlite_freelist_count is not None
            and self.sqlite_freelist_count > self.sqlite_page_count
        ):
            raise ValueError("SQLite freelist 不能超过 page count")
        if self.diagnostic_kind and self.actionable:
            raise ValueError("只读诊断项不能 actionable")


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
