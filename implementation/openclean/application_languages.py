"""应用语言包的保守只读审计。

只枚举应用主资源目录下的 ``*.lproj``。候选必须可确认仅包含字符串资源；任何
未知文件、目录、符号链接、云占位或受保护后代都会使整个语言目录被跳过。由于修改
应用包可能影响代码签名、完整性和后续更新，本模块产出的候选永远不可执行。
"""
from __future__ import annotations

import os
import plistlib
import stat
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from .macos import nonprivileged_action_block_reason
from .models import FileFacts, Item, ScanIssue, ScanResult, normalize_path
from .predicates import Predicate, ProtectionGate

_DEFAULTS_PATH = "/usr/bin/defaults"
_MAX_INFO_PLIST_BYTES = 4 * 1024 * 1024
_STRING_RESOURCE_SUFFIXES = frozenset({".strings", ".stringsdict"})
_READ_ONLY_REASON = (
    "修改应用包内资源存在代码签名、完整性和更新风险，当前仅提供只读审计"
)
_LANGUAGE_ALIASES = {
    "english": "en",
    "chinese": "zh",
    "simplified-chinese": "zh-hans",
    "traditional-chinese": "zh-hant",
    "japanese": "ja",
    "korean": "ko",
    "french": "fr",
    "german": "de",
    "spanish": "es",
    "italian": "it",
    "portuguese": "pt",
}


@dataclass(frozen=True)
class LanguagePreferencesDiscovery:
    languages: tuple[str, ...] = ()
    issues: tuple[ScanIssue, ...] = ()


@dataclass(frozen=True)
class _LocalizationMeasurement:
    logical_size: int
    allocated_size: int


def _preferences_issue(message: str) -> LanguagePreferencesDiscovery:
    return LanguagePreferencesDiscovery(
        issues=(
            ScanIssue(
                code="language_preferences_failed",
                message=message,
                task="应用语言包",
            ),
        )
    )


def discover_preferred_languages(
    *,
    defaults_path: str = _DEFAULTS_PATH,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] | None = None,
    timeout: float = 2.0,
) -> LanguagePreferencesDiscovery:
    """通过 macOS preferences domain 只读获取 ``AppleLanguages``。"""
    command = [defaults_path, "export", "NSGlobalDomain", "-"]
    run = runner or subprocess.run
    try:
        completed = run(
            command,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        return _preferences_issue(f"未找到 defaults：{exc}")
    except subprocess.TimeoutExpired:
        return _preferences_issue(f"defaults 在 {timeout:g} 秒内未完成")
    except OSError as exc:
        return _preferences_issue(f"无法运行 defaults：{exc}")

    if completed.returncode != 0:
        stderr = completed.stderr
        detail = (
            stderr.decode("utf-8", errors="replace").strip()
            if isinstance(stderr, bytes)
            else str(stderr).strip()
        )
        return _preferences_issue(detail or f"defaults 退出码 {completed.returncode}")

    stdout = completed.stdout
    payload = stdout.encode("utf-8") if isinstance(stdout, str) else stdout
    try:
        preferences = plistlib.loads(payload)
    except (ValueError, TypeError, plistlib.InvalidFileException) as exc:
        return _preferences_issue(f"无法解析全局语言偏好：{exc}")
    if not isinstance(preferences, dict):
        return _preferences_issue("全局语言偏好不是 plist 字典")

    raw_languages = preferences.get("AppleLanguages")
    if not isinstance(raw_languages, list) or not raw_languages:
        return _preferences_issue("AppleLanguages 缺失或为空")
    if any(not isinstance(value, str) or not value.strip() for value in raw_languages):
        return _preferences_issue("AppleLanguages 包含无效语言标识")

    languages = tuple(dict.fromkeys(value.strip() for value in raw_languages))
    return LanguagePreferencesDiscovery(languages=languages)


def _language_key(value: str) -> str:
    key = value.strip()
    if key.casefold().endswith(".lproj"):
        key = key[:-6]
    key = key.replace("_", "-").replace(" ", "-").casefold()
    while "--" in key:
        key = key.replace("--", "-")
    return _LANGUAGE_ALIASES.get(key, key)


def _language_is_kept(candidate: str, kept_languages: Iterable[str]) -> bool:
    candidate_key = _language_key(candidate)
    if candidate_key == "base":
        return True
    candidate_base = candidate_key.split("-", 1)[0]
    for language in kept_languages:
        kept_key = _language_key(language)
        if candidate_key == kept_key:
            return True
        # 保留同一基础语言的地区/文字变体。多留一点比误报可删更安全。
        if candidate_base and candidate_base == kept_key.split("-", 1)[0]:
            return True
    return False


def _issue_for_os_error(error: OSError, path: Path, task: str) -> ScanIssue:
    if isinstance(error, PermissionError):
        code = "permission_denied"
    elif isinstance(error, FileNotFoundError):
        code = "path_disappeared"
    else:
        code = "filesystem_error"
    return ScanIssue(code=code, message=str(error), task=task, path=path)


def _dataless_issue(path: Path, task: str) -> ScanIssue:
    return ScanIssue(
        code="dataless_object_skipped",
        message=(
            "检测到 macOS dataless/疑似云占位对象；"
            "为避免触发云端 materialization，已跳过"
        ),
        task=task,
        path=path,
        blocking=False,
    )


def _facts(
    path: Path,
    issues: list[ScanIssue],
    task: str,
    *,
    missing_is_issue: bool,
) -> FileFacts | None:
    try:
        stat_result = path.lstat()
    except FileNotFoundError as exc:
        if missing_is_issue:
            issues.append(_issue_for_os_error(exc, path, task))
        return None
    except (PermissionError, OSError) as exc:
        issues.append(_issue_for_os_error(exc, path, task))
        return None
    return FileFacts(path=path, stat=stat_result)


def _is_ignored(
    protection: Predicate,
    facts: FileFacts,
) -> bool:
    if (
        isinstance(protection, ProtectionGate)
        and protection.knowledge_base_ignores(facts.path)
    ):
        return True
    return protection.should_ignore(facts)


def _discover_applications(
    roots: Iterable[str | os.PathLike[str]],
    protection: Predicate,
    issues: list[ScanIssue],
    checkpoint: Callable[[], None],
    on_progress: Callable[[], None],
    task: str,
) -> list[Path]:
    applications: set[Path] = set()
    for raw_root in roots:
        checkpoint()
        on_progress()
        root = normalize_path(raw_root)
        root_facts = _facts(root, issues, task, missing_is_issue=False)
        if (
            root_facts is None
            or _is_ignored(protection, root_facts)
            or stat.S_ISLNK(root_facts.stat.st_mode)
            or not stat.S_ISDIR(root_facts.stat.st_mode)
        ):
            continue
        if root_facts.is_dataless:
            issues.append(_dataless_issue(root_facts.path, task))
            continue

        # 允许一个普通容器目录（如 /Applications/Utilities），但绝不进入 .app。
        pending: list[tuple[Path, int]] = [(root, 0)]
        while pending:
            directory, depth = pending.pop()
            checkpoint()
            directory_facts = _facts(
                directory,
                issues,
                task,
                missing_is_issue=True,
            )
            if (
                directory_facts is None
                or _is_ignored(protection, directory_facts)
                or stat.S_ISLNK(directory_facts.stat.st_mode)
                or not stat.S_ISDIR(directory_facts.stat.st_mode)
            ):
                continue
            if directory_facts.is_dataless:
                issues.append(_dataless_issue(directory_facts.path, task))
                continue
            try:
                with os.scandir(directory) as iterator:
                    entries = sorted(iterator, key=lambda entry: entry.name)
            except (PermissionError, FileNotFoundError, OSError) as exc:
                issues.append(_issue_for_os_error(exc, directory, task))
                continue
            for entry in entries:
                checkpoint()
                on_progress()
                if entry.name.startswith("."):
                    continue
                path = normalize_path(entry.path)
                entry_facts = _facts(path, issues, task, missing_is_issue=True)
                if (
                    entry_facts is None
                    or _is_ignored(protection, entry_facts)
                    or stat.S_ISLNK(entry_facts.stat.st_mode)
                    or not stat.S_ISDIR(entry_facts.stat.st_mode)
                ):
                    continue
                if entry_facts.is_dataless:
                    issues.append(_dataless_issue(entry_facts.path, task))
                    continue
                if path.name.casefold().endswith(".app"):
                    applications.add(path)
                elif depth == 0:
                    pending.append((path, depth + 1))
    return sorted(applications, key=str)


def _development_region(
    application: Path,
    issues: list[ScanIssue],
    task: str,
) -> str | None:
    contents = application / "Contents"
    contents_facts = _facts(contents, issues, task, missing_is_issue=False)
    if (
        contents_facts is None
        or stat.S_ISLNK(contents_facts.stat.st_mode)
        or not stat.S_ISDIR(contents_facts.stat.st_mode)
    ):
        issues.append(
            ScanIssue(
                code="application_metadata_invalid",
                message="Contents 目录缺失或类型无效，已跳过应用语言审计",
                task=task,
                path=contents,
            )
        )
        return None
    if contents_facts.is_dataless:
        issues.append(_dataless_issue(contents, task))
        return None

    info_path = contents / "Info.plist"
    info_facts = _facts(info_path, issues, task, missing_is_issue=False)
    if info_facts is None:
        issues.append(
            ScanIssue(
                code="application_metadata_invalid",
                message="Info.plist 缺失，已跳过应用语言审计",
                task=task,
                path=info_path,
            )
        )
        return None
    if (
        stat.S_ISLNK(info_facts.stat.st_mode)
        or not stat.S_ISREG(info_facts.stat.st_mode)
        or info_facts.stat.st_size > _MAX_INFO_PLIST_BYTES
    ):
        issues.append(
            ScanIssue(
                code="application_metadata_invalid",
                message="Info.plist 类型或大小无效，已跳过应用语言审计",
                task=task,
                path=info_path,
            )
        )
        return None
    if info_facts.is_probable_cloud_placeholder:
        issues.append(_dataless_issue(info_path, task))
        return None
    try:
        metadata = plistlib.loads(info_path.read_bytes())
    except (PermissionError, OSError) as exc:
        issues.append(_issue_for_os_error(exc, info_path, task))
        return None
    except (ValueError, TypeError, plistlib.InvalidFileException) as exc:
        issues.append(
            ScanIssue(
                code="application_metadata_invalid",
                message=f"无法解析 Info.plist：{exc}",
                task=task,
                path=info_path,
            )
        )
        return None
    if not isinstance(metadata, dict):
        issues.append(
            ScanIssue(
                code="application_metadata_invalid",
                message="Info.plist 不是字典，已跳过应用语言审计",
                task=task,
                path=info_path,
            )
        )
        return None
    region = metadata.get("CFBundleDevelopmentRegion")
    if region is None:
        issues.append(
            ScanIssue(
                code="application_metadata_invalid",
                message=(
                    "Info.plist 缺少 CFBundleDevelopmentRegion，"
                    "已跳过应用语言审计"
                ),
                task=task,
                path=info_path,
            )
        )
        return None
    if not isinstance(region, str) or not region.strip():
        issues.append(
            ScanIssue(
                code="application_metadata_invalid",
                message="CFBundleDevelopmentRegion 无效，已跳过应用语言审计",
                task=task,
                path=info_path,
            )
        )
        return None
    return region.strip()


def _measure_strings_only(
    localization: FileFacts,
    protection: Predicate,
    issues: list[ScanIssue],
    checkpoint: Callable[[], None],
    on_progress: Callable[[], None],
    task: str,
) -> _LocalizationMeasurement | None:
    latest = _facts(
        localization.path,
        issues,
        task,
        missing_is_issue=True,
    )
    if (
        latest is None
        or _is_ignored(protection, latest)
        or stat.S_ISLNK(latest.stat.st_mode)
        or not stat.S_ISDIR(latest.stat.st_mode)
    ):
        return None
    if latest.is_dataless:
        issues.append(_dataless_issue(latest.path, task))
        return None
    try:
        with os.scandir(latest.path) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name)
    except (PermissionError, FileNotFoundError, OSError) as exc:
        issues.append(_issue_for_os_error(exc, localization.path, task))
        return None

    logical_size = 0
    allocated_size = 0
    string_files = 0
    seen_inodes: set[tuple[int, int]] = set()
    for entry in entries:
        checkpoint()
        on_progress()
        path = normalize_path(entry.path)
        facts = _facts(path, issues, task, missing_is_issue=True)
        if facts is None or _is_ignored(protection, facts):
            return None
        if stat.S_ISLNK(facts.stat.st_mode) or not stat.S_ISREG(facts.stat.st_mode):
            return None
        if path.suffix.casefold() not in _STRING_RESOURCE_SUFFIXES:
            return None
        if facts.is_probable_cloud_placeholder:
            return None
        inode = (facts.stat.st_dev, facts.stat.st_ino)
        if facts.stat.st_nlink > 1 and inode in seen_inodes:
            continue
        seen_inodes.add(inode)
        string_files += 1
        logical_size += facts.logical_size
        allocated_size += facts.allocated_size

    if string_files == 0 or allocated_size == 0:
        return None
    return _LocalizationMeasurement(
        logical_size=logical_size,
        allocated_size=allocated_size,
    )


def scan_application_languages(
    roots: Iterable[str | os.PathLike[str]],
    protection: Predicate,
    *,
    preferred_languages: Iterable[str] | None = None,
    category: str = "应用语言包",
    context_note: str = "",
    checkpoint: Callable[[], None] | None = None,
    on_progress: Callable[[], None] | None = None,
    preferences_runner: Callable[..., subprocess.CompletedProcess[bytes]] | None = None,
) -> ScanResult:
    """审计未匹配用户语言、且仅含字符串资源的 ``.lproj`` 目录。"""
    check = checkpoint or (lambda: None)
    advance = on_progress or (lambda: None)
    result = ScanResult()

    if preferred_languages is None:
        discovery = discover_preferred_languages(runner=preferences_runner)
        result.issues.extend(discovery.issues)
        languages = discovery.languages
    else:
        languages = tuple(
            dict.fromkeys(
                value.strip()
                for value in preferred_languages
                if isinstance(value, str) and value.strip()
            )
        )
        if not languages:
            result.issues.append(
                ScanIssue(
                    code="language_preferences_failed",
                    message="没有可用的首选语言，已跳过应用语言审计",
                    task=category,
                )
            )
    if not languages:
        return result

    applications = _discover_applications(
        roots,
        protection,
        result.issues,
        check,
        advance,
        category,
    )
    for application in applications:
        check()
        application_facts = _facts(
            application,
            result.issues,
            category,
            missing_is_issue=True,
        )
        if (
            application_facts is None
            or _is_ignored(protection, application_facts)
            or stat.S_ISLNK(application_facts.stat.st_mode)
            or not stat.S_ISDIR(application_facts.stat.st_mode)
        ):
            continue
        if application_facts.is_dataless:
            result.issues.append(_dataless_issue(application_facts.path, category))
            continue
        development_region = _development_region(
            application,
            result.issues,
            category,
        )
        if development_region is None:
            continue
        kept_languages = (*languages, development_region, "Base")
        resources = application / "Contents" / "Resources"
        resources_facts = _facts(
            resources,
            result.issues,
            category,
            missing_is_issue=False,
        )
        if (
            resources_facts is None
            or _is_ignored(protection, resources_facts)
            or stat.S_ISLNK(resources_facts.stat.st_mode)
            or not stat.S_ISDIR(resources_facts.stat.st_mode)
        ):
            continue
        if resources_facts.is_dataless:
            result.issues.append(_dataless_issue(resources_facts.path, category))
            continue
        try:
            with os.scandir(resources) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except (PermissionError, FileNotFoundError, OSError) as exc:
            result.issues.append(_issue_for_os_error(exc, resources, category))
            continue

        audited_localizations: list[tuple[Path, _LocalizationMeasurement]] = []
        for entry in entries:
            check()
            advance()
            if not entry.name.casefold().endswith(".lproj"):
                continue
            path = normalize_path(entry.path)
            localization = _facts(
                path,
                result.issues,
                category,
                missing_is_issue=True,
            )
            if (
                localization is None
                or _is_ignored(protection, localization)
                or stat.S_ISLNK(localization.stat.st_mode)
                or not stat.S_ISDIR(localization.stat.st_mode)
                or _language_is_kept(path.stem, kept_languages)
            ):
                continue
            if localization.is_dataless:
                result.issues.append(_dataless_issue(localization.path, category))
                continue
            measurement = _measure_strings_only(
                localization,
                protection,
                result.issues,
                check,
                advance,
                category,
            )
            if measurement is None:
                continue
            audited_localizations.append((path, measurement))

        if audited_localizations:
            language_names = [path.stem for path, _ in audited_localizations]
            logical_size = sum(
                measurement.logical_size
                for _, measurement in audited_localizations
            )
            allocated_size = sum(
                measurement.allocated_size
                for _, measurement in audited_localizations
            )
            note_parts = (
                context_note,
                f"候选语言（{len(language_names)}）：{', '.join(language_names)}",
                "候选目录均仅含 .strings/.stringsdict 本地化资源",
                _READ_ONLY_REASON,
            )
            result.items.append(
                Item(
                    path=application,
                    size=allocated_size,
                    category=category,
                    safety="critical",
                    note="；".join(part for part in note_parts if part),
                    logical_size=logical_size,
                    allocated_size=allocated_size,
                    actionable=False,
                    action_block_reason=_READ_ONLY_REASON,
                    requires_privilege=bool(
                        nonprivileged_action_block_reason(application)
                    ),
                    identity=application_facts.identity,
                    artifact_name=",".join(language_names),
                    total_count=len(language_names),
                    preselected=False,
                )
            )

    result.items.sort(key=lambda item: str(item.path))
    return result
