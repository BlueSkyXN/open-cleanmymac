"""Homebrew Caskroom 旧版本目录扫描。

只比较 cask 安装回执（``Caskroom/<token>/.metadata/INSTALL_RECEIPT.json`` 的
``source.version``）与同一 token 下的顶层版本目录。目录版本严格低于回执版本
才识别为升级残留；当前版本、更高版本、无法比较或回执缺失一律不产生候选。
回执是 Homebrew 安装时写入的公开状态文件，版本判定原语复用 updater 模块。
"""
from __future__ import annotations

import json
import os
import re
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .filesystem import lstat_retry, scandir_entries
from .models import FileIdentity, Item, ScanIssue, ScanResult, normalize_path
from .processes import (
    OpenFileDetectionError,
    OpenFileSnapshot,
    ProcessDetectionError,
    ProcessSnapshot,
    capture_open_file_snapshot,
    capture_process_snapshot,
)
from .storage_diagnostics import _candidate_measurement
from .updater import _compare_versions

CASKROOM_SCAN_TASK = "homebrew-caskroom"
_MAX_RECEIPT_BYTES = 2 * 1024 * 1024
_MAX_TOKENS = 20_000
_TOKEN_NAME = re.compile(r"^[a-z0-9][a-z0-9@._+-]*$")


def caskroom_roots() -> tuple[Path, ...]:
    """Caskroom 候选根：默认安装位置加 HOMEBREW_PREFIX 覆盖。"""
    roots = [Path("/opt/homebrew/Caskroom"), Path("/usr/local/Caskroom")]
    prefix = os.environ.get("HOMEBREW_PREFIX")
    if prefix:
        path = normalize_path(prefix)
        if path.is_absolute():
            roots.append(path / "Caskroom")
    return tuple(dict.fromkeys(roots))


@dataclass(frozen=True)
class CaskVersionState:
    """一个 token 的回执版本与目录版本比较结果。"""

    token: str
    receipt_version: str
    directory_version: str
    status: str
    block_reason: str = ""
    note: str = ""

    @property
    def blocks_cleanup(self) -> bool:
        return bool(self.block_reason)


def _receipt_version(token_root: Path) -> tuple[str, str]:
    """读取回执版本；返回 ``(version, error)``，两者互斥。"""
    receipt = token_root / ".metadata" / "INSTALL_RECEIPT.json"
    try:
        receipt_stat = receipt.lstat()
    except FileNotFoundError:
        return "", "缺少 INSTALL_RECEIPT.json，无法确认当前版本"
    except OSError as exc:
        return "", f"无法读取安装回执：{exc}"
    if (stat.S_ISLNK(receipt_stat.st_mode) or not stat.S_ISREG(receipt_stat.st_mode)
            or receipt_stat.st_size > _MAX_RECEIPT_BYTES):
        return "", "安装回执不是普通文件或超出大小上限"
    try:
        payload = json.loads(receipt.read_bytes())
    except (OSError, ValueError):
        return "", "安装回执无法解析为 JSON"
    source = payload.get("source") if isinstance(payload, dict) else None
    version = source.get("version") if isinstance(source, dict) else None
    if not isinstance(version, str) or not version.strip() or len(version) > 256:
        return "", "安装回执缺少有效的 source.version"
    return version.strip(), ""


def assess_cask_version(token: str, receipt_version: str, directory_version: str) -> CaskVersionState:
    """按回执版本判定目录版本状态；语义与 updater 版本残留一致。"""
    comparison = _compare_versions(directory_version, receipt_version)
    if comparison is None:
        return CaskVersionState(
            token, receipt_version, directory_version, "version_unknown",
            "目录或回执版本不是纯数字版本号，无法比较",
            f"目录版本 {directory_version} 无法与回执 {receipt_version} 比较，强制保护",
        )
    if comparison > 0:
        return CaskVersionState(
            token, receipt_version, directory_version, "pending_update",
            "目录版本高于回执版本，可能正在升级",
            f"目录 {directory_version} 高于回执 {receipt_version}，疑似升级进行中，强制保护",
        )
    if comparison == 0:
        return CaskVersionState(
            token, receipt_version, directory_version, "same_version_residue",
            block_reason="目录版本与回执一致，是当前安装版本",
        )
    return CaskVersionState(
        token, receipt_version, directory_version, "older_version_residue",
        note=f"目录 {directory_version} 低于回执 {receipt_version}，属于升级后残留",
    )


def _item_updater_fields(state: CaskVersionState) -> dict[str, object]:
    """把版本状态映射到 Item 的 updater 复核四元组，执行前重新比较。"""
    return {
        "updater_status": state.status,
        "installed_version": state.receipt_version,
        "staged_version": state.directory_version,
    }


def scan_caskroom_versions(
    protection,
    *,
    roots: tuple[Path, ...] | None = None,
    process_snapshot: ProcessSnapshot | None = None,
    open_files: OpenFileSnapshot | None = None,
    snapshots: tuple[ProcessSnapshot | None, OpenFileSnapshot | None] | None = None,
    now: float | None = None,
    checkpoint: Callable[[], None] = lambda: None,
) -> ScanResult:
    """枚举各 Caskroom 根下带旧版本目录的 token；不修改任何状态。"""
    from .engine import Cancelled

    result = ScanResult()
    if snapshots is None:
        try:
            processes: ProcessSnapshot | None = capture_process_snapshot()
        except ProcessDetectionError:
            processes = None
            result.issues.append(ScanIssue(
                code="process_detection_failed",
                message="无法获取进程快照；相关候选保持不可执行",
                task=CASKROOM_SCAN_TASK, blocking=False,
            ))
        try:
            handles: OpenFileSnapshot | None = capture_open_file_snapshot()
        except OpenFileDetectionError:
            handles = None
            result.issues.append(ScanIssue(
                code="open_file_detection_failed",
                message="无法获取打开文件快照；相关候选保持不可执行",
                task=CASKROOM_SCAN_TASK, blocking=False,
            ))
    else:
        processes, handles = snapshots
    brew_running = processes is not None and _brew_active(processes)
    observed_at = time.time() if now is None else now
    scanned_tokens = 0
    try:
        for raw_root in (roots if roots is not None else caskroom_roots()):
            root = normalize_path(raw_root)
            checkpoint()
            try:
                root_stat = lstat_retry(root)
            except FileNotFoundError:
                continue
            except OSError as exc:
                result.issues.append(_fs_issue(exc, root))
                continue
            if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
                continue
            try:
                tokens = sorted(scandir_entries(root), key=lambda entry: entry.name)
            except OSError as exc:
                result.issues.append(_fs_issue(exc, root))
                continue
            for entry in tokens:
                checkpoint()
                if scanned_tokens >= _MAX_TOKENS:
                    result.issues.append(ScanIssue(
                        code="caskroom_token_limit", message="Caskroom token 数超过上限",
                        task=CASKROOM_SCAN_TASK, blocking=True,
                    ))
                    return result
                if not _TOKEN_NAME.fullmatch(entry.name):
                    continue
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                scanned_tokens += 1
                _scan_token(Path(entry.path), root_stat.st_dev, protection, result,
                            brew_running=brew_running, now=observed_at,
                            process_snapshot=processes, open_files=handles)
    except Cancelled:
        result.cancelled = True
        raise
    return result


def _brew_active(snapshot: ProcessSnapshot) -> bool:
    from .homebrew import running

    return running(snapshot)


def _scan_token(
    token_root: Path,
    root_device: int,
    protection,
    result: ScanResult,
    *,
    brew_running: bool,
    now: float,
    process_snapshot: ProcessSnapshot | None,
    open_files: OpenFileSnapshot | None,
) -> None:
    task = CASKROOM_SCAN_TASK
    try:
        entries = sorted(scandir_entries(token_root), key=lambda item: item.name)
    except OSError as exc:
        result.issues.append(_fs_issue(exc, token_root))
        return
    versions = []
    for entry in entries:
        if entry.name.startswith("."):
            continue
        try:
            if entry.is_dir(follow_symlinks=False):
                versions.append(entry.name)
        except OSError:
            continue
    if len(versions) <= 1:
        return
    receipt_version, receipt_error = _receipt_version(token_root)
    for directory_version in versions:
        if receipt_error:
            state = CaskVersionState(
                token_root.name, "", directory_version, "version_unknown",
                receipt_error,
                f"{token_root.name}：{receipt_error}；强制保护",
            )
        else:
            state = assess_cask_version(token_root.name, receipt_version, directory_version)
        path = token_root / directory_version
        try:
            before = lstat_retry(path)
        except OSError as exc:
            result.issues.append(_fs_issue(exc, path))
            continue
        issue_start = len(result.issues)
        measurement = _candidate_measurement(
            path, root_device, protection, result.issues, task=task, now=now,
        )
        if measurement is None:
            continue
        try:
            after = lstat_retry(path)
        except OSError as exc:
            result.issues.append(_fs_issue(exc, path))
            continue
        if (before.st_dev, before.st_ino, before.st_uid, before.st_mode,
                before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_dev, after.st_ino, after.st_uid, after.st_mode,
                after.st_mtime_ns, after.st_ctime_ns):
            result.issues.append(ScanIssue(
                code="target_changed_during_scan", message="目标在计量期间变化",
                task=task, path=path, blocking=True,
            ))
            continue
        open_handles = (
            open_files.count_under(path) if open_files is not None else None
        )
        newest_mtime = measurement.newest_mtime or None
        age_days = (
            max(0, int((now - newest_mtime) // 86400))
            if newest_mtime is not None else None
        )
        block_reasons: list[str] = []
        if len(result.issues) > issue_start:
            block_reasons.append("measurement_incomplete")
        if open_handles is None:
            block_reasons.append("打开句柄状态未知")
        elif open_handles:
            block_reasons.append("存在打开句柄")
        if process_snapshot is None:
            block_reasons.append("进程状态未知")
        elif brew_running:
            block_reasons.append("Homebrew 正在运行")
        if state.blocks_cleanup:
            block_reasons.append(state.block_reason)
        if measurement.excluded_paths:
            block_reasons.append("包含忽略或保护路径")
        if measurement.cross_device_paths:
            block_reasons.append("包含其它文件系统挂载点")
        if measurement.cloud_file_count:
            block_reasons.append("包含云占位文件")
        actionable = not block_reasons
        age_label = age_days if age_days is not None else "?"
        notes = [
            f"Homebrew cask {token_root.name} 旧版本目录（{age_label} 天）",
            state.note,
        ]
        if state.status == "older_version_residue":
            notes.append(
                "删除旧版本不影响当前安装；等价于 brew cleanup --cask 的范围，"
                "不会卸载 token 或触碰 .metadata",
            )
        item = Item(
            path,
            measurement.allocated_bytes,
            "Homebrew Cask 旧版本",
            "critical",
            "；".join(part for part in notes if part),
            logical_size=measurement.logical_bytes,
            allocated_size=measurement.allocated_bytes,
            actionable=actionable,
            action_block_reason="" if actionable else "；".join(block_reasons),
            identity=FileIdentity.from_stat(after),
            latest_mtime=newest_mtime,
            age_days=age_days,
            preselected=False,
            resource_kind="filesystem",
            excluded_paths=measurement.excluded_paths,
            cross_device_paths=measurement.cross_device_paths,
            cloud_file_count=measurement.cloud_file_count,
            requires_explicit_selection=True,
            domain="developer",
            running_process_markers=("brew", "brew.rb", "brew.sh"),
            open_handle_count=open_handles,
            cleanup_scope="homebrew-caskroom",
            cleanup_root=token_root,
            cleanup_root_identity=FileIdentity.from_stat(
                _safe_lstat(token_root, before)
            ),
            **_item_updater_fields(state),
        )
        result.items.append(item)


def _safe_lstat(path: Path, fallback: os.stat_result) -> os.stat_result:
    try:
        return lstat_retry(path)
    except OSError:
        return fallback


def _fs_issue(exc: OSError, path: Path) -> ScanIssue:
    return ScanIssue(
        code="permission_denied" if isinstance(exc, PermissionError) else "filesystem_error",
        message=str(exc), task=CASKROOM_SCAN_TASK, path=path,
    )
