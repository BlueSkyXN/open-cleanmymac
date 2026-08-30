"""公开 updater 缓存的只读版本状态判定。"""
from __future__ import annotations

import os
import plistlib
import re
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .models import UPDATER_STATUSES, normalize_path

_MAX_INFO_PLIST_BYTES = 2 * 1024 * 1024
_NUMERIC_VERSION = re.compile(r"^\d+(?:\.\d+)*$")


@dataclass(frozen=True)
class UpdaterRule:
    relative_root: str
    bundle_id: str
    staged_app_globs: tuple[str, ...] = ()
    staged_archive_globs: tuple[str, ...] = ()


UPDATER_RULES: tuple[UpdaterRule, ...] = (
    UpdaterRule(
        "Library/Caches/com.openai.codex",
        "com.openai.codex",
        staged_app_globs=(
            "org.sparkle-project.Sparkle/Installation/*/*/ChatGPT.app",
        ),
        staged_archive_globs=(
            "org.sparkle-project.Sparkle/Installation/*/*.zip",
        ),
    ),
    UpdaterRule(
        "Library/Caches/com.workbuddy.workbuddy.BundleMigration",
        "com.workbuddy.workbuddy",
        staged_app_globs=("extracted/*/WorkBuddy.app",),
        staged_archive_globs=("downloads/WorkBuddy-*.zip",),
    ),
    UpdaterRule(
        "Library/Caches/com.aliyun.lingma.ide.ShipIt",
        "com.aliyun.lingma.ide",
        staged_app_globs=("update.*/Qoder CN IDE.app",),
    ),
    UpdaterRule(
        "Library/Caches/qoder-cn-updater",
        "com.qodercn.app",
        staged_archive_globs=(
            "pending/Qoder-CN-mac-arm64.zip",
            "update.zip",
        ),
    ),
    UpdaterRule(
        "Library/Application Support/LarkShell/update",
        "com.electron.lark",
        staged_app_globs=("update.noindex/Lark.app",),
        staged_archive_globs=("update_downloading/*.zip",),
    ),
    UpdaterRule(
        "Library/Caches/TRAE SOLO CN",
        "cn.trae.solo.app",
        staged_archive_globs=(
            "pending/TraeWork_CN-darwin-arm64.zip",
            "update.zip",
        ),
    ),
)


@dataclass(frozen=True)
class _BundleMetadata:
    bundle_id: str
    version: str
    path: Path | None = None


@dataclass(frozen=True)
class UpdaterAssessment:
    status: str
    bundle_id: str
    staged_version: str = ""
    installed_version: str = ""
    external_install: bool = False

    def __post_init__(self) -> None:
        if not self.status or self.status not in UPDATER_STATUSES:
            raise ValueError(f"未知 updater 状态：{self.status}")

    @property
    def blocks_cleanup(self) -> bool:
        return self.status in {
            "pending_update",
            "installed_app_missing",
            "version_unknown",
        }

    @property
    def block_reason(self) -> str:
        reasons = {
            "pending_update": "缓存包含尚未安装的新版本",
            "installed_app_missing": "找不到对应的已安装应用",
            "version_unknown": "无法可靠确认 updater 版本状态",
        }
        return reasons.get(self.status, "")

    @property
    def note(self) -> str:
        labels = {
            "pending_update": "检测到待安装的新版本，强制保护",
            "same_version_residue": "暂存版本与已安装版本相同，仅可精确审阅",
            "older_version_residue": "暂存版本低于已安装版本，仅可精确审阅",
            "installed_app_missing": "未找到对应已安装应用，状态不明且强制保护",
            "version_unknown": "无法可靠读取或比较版本，强制保护",
        }
        detail = labels[self.status]
        if self.installed_version or self.staged_version:
            detail += (
                f"（installed={self.installed_version or 'unknown'}, "
                f"staged={self.staged_version or 'unknown'}）"
            )
        if self.external_install:
            detail += "；应用位于外置卷，自动更新可能反复下载"
        return detail


def _clean_version(value: object) -> str:
    if not isinstance(value, (str, int, float)):
        return ""
    version = str(value).strip()
    return version if 0 < len(version) <= 128 else ""


def _bundle_metadata(payload: object, path: Path | None = None) -> _BundleMetadata | None:
    if not isinstance(payload, dict):
        return None
    bundle_id = payload.get("CFBundleIdentifier")
    if not isinstance(bundle_id, str) or not bundle_id.strip():
        return None
    version = _clean_version(payload.get("CFBundleShortVersionString"))
    if not version:
        version = _clean_version(payload.get("CFBundleVersion"))
    if not version:
        return None
    return _BundleMetadata(bundle_id.strip(), version, path)


def _read_app_metadata(path: Path) -> _BundleMetadata | None:
    try:
        if path.is_symlink() or not stat.S_ISDIR(path.lstat().st_mode):
            return None
        info = path / "Contents" / "Info.plist"
        info_stat = info.lstat()
        if (
            stat.S_ISLNK(info_stat.st_mode)
            or not stat.S_ISREG(info_stat.st_mode)
            or info_stat.st_size > _MAX_INFO_PLIST_BYTES
        ):
            return None
        payload = plistlib.loads(info.read_bytes())
    except (
        FileNotFoundError,
        PermissionError,
        OSError,
        ValueError,
        TypeError,
        plistlib.InvalidFileException,
    ):
        return None
    return _bundle_metadata(payload, path)


def _read_archive_metadata(
    path: Path,
    expected_bundle_id: str,
) -> _BundleMetadata | None:
    try:
        if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
            return None
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                if (
                    not member.filename.endswith(".app/Contents/Info.plist")
                    or member.filename.count(".app/") != 1
                    or member.file_size > _MAX_INFO_PLIST_BYTES
                ):
                    continue
                metadata = _bundle_metadata(
                    plistlib.loads(archive.read(member)),
                )
                if metadata is not None and metadata.bundle_id == expected_bundle_id:
                    return metadata
    except (
        FileNotFoundError,
        PermissionError,
        OSError,
        ValueError,
        TypeError,
        EOFError,
        OverflowError,
        RuntimeError,
        zipfile.BadZipFile,
        plistlib.InvalidFileException,
    ):
        return None
    return None


def _application_roots(home: Path) -> tuple[Path, ...]:
    roots = [Path("/Applications"), home / "Applications"]
    volumes = Path("/Volumes")
    try:
        entries = tuple(os.scandir(volumes))
    except (FileNotFoundError, PermissionError, OSError):
        entries = ()
    for entry in entries:
        try:
            if entry.is_dir(follow_symlinks=False):
                roots.append(Path(entry.path) / "Applications")
        except OSError:
            continue
    return tuple(dict.fromkeys(roots))


def _installed_metadata(
    bundle_id: str,
    roots: tuple[Path, ...],
) -> tuple[_BundleMetadata, ...]:
    found: list[_BundleMetadata] = []
    for root in roots:
        try:
            applications = tuple(root.glob("*.app"))
        except OSError:
            continue
        for application in applications:
            metadata = _read_app_metadata(application)
            if metadata is not None and metadata.bundle_id == bundle_id:
                found.append(metadata)
    return tuple(found)


def _glob_matches(root: Path, pattern: str) -> tuple[tuple[Path, ...], bool]:
    try:
        return tuple(root.glob(pattern)), False
    except OSError:
        return (), True


def _version_numbers(value: str) -> tuple[int, ...] | None:
    if not _NUMERIC_VERSION.fullmatch(value):
        return None
    numbers = [int(component) for component in value.split(".")]
    while len(numbers) > 1 and numbers[-1] == 0:
        numbers.pop()
    return tuple(numbers)


def _compare_versions(left: str, right: str) -> int | None:
    left_numbers = _version_numbers(left)
    right_numbers = _version_numbers(right)
    if left_numbers is None or right_numbers is None:
        return None
    width = max(len(left_numbers), len(right_numbers))
    left_padded = left_numbers + (0,) * (width - len(left_numbers))
    right_padded = right_numbers + (0,) * (width - len(right_numbers))
    return (left_padded > right_padded) - (left_padded < right_padded)


def _assess_rule(
    candidate: Path,
    rule: UpdaterRule,
    *,
    home: Path,
    application_roots: tuple[Path, ...] | None,
) -> UpdaterAssessment | None:
    staged: list[_BundleMetadata] = []
    unknown_artifact = False
    for pattern in rule.staged_app_globs:
        matches, failed = _glob_matches(candidate, pattern)
        unknown_artifact = unknown_artifact or failed
        for match in matches:
            metadata = _read_app_metadata(match)
            if metadata is None or metadata.bundle_id != rule.bundle_id:
                unknown_artifact = True
            else:
                staged.append(metadata)
    for pattern in rule.staged_archive_globs:
        matches, failed = _glob_matches(candidate, pattern)
        unknown_artifact = unknown_artifact or failed
        for match in matches:
            metadata = _read_archive_metadata(match, rule.bundle_id)
            if metadata is None:
                unknown_artifact = True
            else:
                staged.append(metadata)
    if not staged and not unknown_artifact:
        return None
    if unknown_artifact or not staged:
        return UpdaterAssessment("version_unknown", rule.bundle_id)

    staged_versions = {metadata.version for metadata in staged}
    if len(staged_versions) != 1:
        return UpdaterAssessment("version_unknown", rule.bundle_id)
    staged_version = next(iter(staged_versions))

    roots = (
        _application_roots(home)
        if application_roots is None
        else application_roots
    )
    installed = _installed_metadata(rule.bundle_id, roots)
    if not installed:
        return UpdaterAssessment(
            "installed_app_missing",
            rule.bundle_id,
            staged_version=staged_version,
        )
    installed_versions = {metadata.version for metadata in installed}
    external_install = any(
        metadata.path is not None
        and str(metadata.path).startswith("/Volumes/")
        for metadata in installed
    )
    if len(installed_versions) != 1:
        return UpdaterAssessment(
            "version_unknown",
            rule.bundle_id,
            staged_version=staged_version,
            external_install=external_install,
        )
    installed_version = next(iter(installed_versions))
    comparison = _compare_versions(staged_version, installed_version)
    if comparison is None:
        status = "version_unknown"
    elif comparison > 0:
        status = "pending_update"
    elif comparison == 0:
        status = "same_version_residue"
    else:
        status = "older_version_residue"
    return UpdaterAssessment(
        status,
        rule.bundle_id,
        staged_version=staged_version,
        installed_version=installed_version,
        external_install=external_install,
    )


def assess_updater_candidate(
    path: str | os.PathLike[str],
    *,
    home: Path | None = None,
    application_roots: tuple[Path, ...] | None = None,
) -> UpdaterAssessment | None:
    """判定已知 updater 根；没有暂存 bundle 时返回 ``None``。"""
    base = normalize_path(home or Path.home())
    candidate = normalize_path(path)
    rule = next(
        (
            current
            for current in UPDATER_RULES
            if candidate == normalize_path(base / current.relative_root)
        ),
        None,
    )
    if rule is None:
        return None
    return _assess_rule(
        candidate,
        rule,
        home=base,
        application_roots=application_roots,
    )


def assess_updater_staging_root(
    path: str | os.PathLike[str],
    *,
    bundle_id: str,
    staged_app_globs: tuple[str, ...] = (),
    staged_archive_globs: tuple[str, ...] = (),
    home: Path | None = None,
    application_roots: tuple[Path, ...] | None = None,
) -> UpdaterAssessment | None:
    """判定动态发现的 updater 暂存根，不要求它位于固定 cache 路径。"""

    base = normalize_path(home or Path.home())
    candidate = normalize_path(path)
    return _assess_rule(
        candidate,
        UpdaterRule(
            "",
            bundle_id,
            staged_app_globs=staged_app_globs,
            staged_archive_globs=staged_archive_globs,
        ),
        home=base,
        application_roots=application_roots,
    )
