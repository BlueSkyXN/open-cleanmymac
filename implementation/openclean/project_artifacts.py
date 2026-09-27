"""项目产物的只读内容保护，不把目录命名当作可重建证明。"""
from __future__ import annotations

import fnmatch
import os
import selectors
import stat
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .filesystem import scandir_entries
from .macos import scan_symlink_anchor
from .models import normalize_path
from .path_inspection import InspectionError, PathInspection
from .predicates import Predicate
from .scanpoints import (
    PROJECT_ARTIFACT_GLOBS,
    PROJECT_ARTIFACT_NAMES,
    PROJECT_ARTIFACT_SUBDIRECTORIES,
)

ZIG_ARTIFACTS = frozenset({".zig-cache", "zig-out"})


@dataclass(frozen=True)
class ArtifactAssessment:
    block_reason: str = ""
    complete: bool = True


def artifact_name(path: Path) -> str:
    if path.name in PROJECT_ARTIFACT_NAMES or any(
        fnmatch.fnmatchcase(path.name, pattern) for pattern in PROJECT_ARTIFACT_GLOBS
    ):
        return path.name
    if path.name in PROJECT_ARTIFACT_SUBDIRECTORIES.get(path.parent.name, ()):
        return f"{path.parent.name}/{path.name}"
    return ""


def project_scope_block_reason(path: Path) -> str:
    for candidate in (normalize_path(path), *normalize_path(path).parents):
        if artifact_name(candidate):
            return "扫描根位于依赖或产物目录内；请改选其所属项目，不扫描已安装依赖内部"
        if candidate.name.casefold() == ".git":
            return "扫描根位于 Git 元数据目录内"
    return ""


def _metadata_directory(path: Path, inspection: PathInspection) -> None:
    facts = inspection.checked_path(path)
    if not stat.S_ISDIR(facts.stat.st_mode):
        raise InspectionError("Git 元数据根不是普通目录")
    for name in ("config", "config.worktree", "index", "HEAD"):
        try:
            metadata = inspection.checked_path(path / name)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(metadata.stat.st_mode):
            raise InspectionError("Git 元数据不是普通文件")
    # Split-index 会额外读取 sharedindex 文件，不能只检查主 index。
    for entry in scandir_entries(path):
        inspection.tick()
        if entry.name.startswith("sharedindex."):
            metadata = inspection.checked_path(Path(entry.path))
            if not stat.S_ISREG(metadata.stat.st_mode):
                raise InspectionError("Git shared index 不是普通文件")


def _git_directory(marker: Path, inspection: PathInspection) -> Path:
    facts = inspection.checked_path(marker)
    if stat.S_ISDIR(facts.stat.st_mode):
        directory = marker
    else:
        value = inspection.read_metadata(marker)
        if not value.startswith(b"gitdir: ") or b"\0" in value:
            raise InspectionError("Git worktree 定位文件无效")
        raw = os.fsdecode(value[8:].rstrip(b"\r\n"))
        if not raw or "\n" in raw:
            raise InspectionError("Git worktree 定位文件无效")
        directory = normalize_path(marker.parent / raw)
    _metadata_directory(directory, inspection)
    try:
        value = inspection.read_metadata(directory / "commondir")
    except FileNotFoundError:
        return directory
    raw = os.fsdecode(value.rstrip(b"\r\n"))
    if not raw or "\n" in raw or "\0" in raw:
        raise InspectionError("Git common directory 定位文件无效")
    _metadata_directory(normalize_path(directory / raw), inspection)
    return directory


def _tracked_content(
    path: Path, repository: Path, directory: Path, inspection: PathInspection,
) -> bool:
    environment = {
        "PATH": "/usr/bin:/bin", "LC_ALL": "C", "HOME": str(Path.home()),
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0", "GIT_LITERAL_PATHSPECS": "1", "GIT_NO_LAZY_FETCH": "1",
    }
    command = [
        "/usr/bin/git", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
        "-c", "core.preloadIndex=false", "-c", "core.hooksPath=/dev/null",
        f"--git-dir={directory}", f"--work-tree={repository}",
        "ls-files", "--cached", "-z", "--", path.relative_to(repository).as_posix() + "/",
    ]
    process = subprocess.Popen(
        command, cwd=repository, env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    try:
        assert process.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                # Git 子进程不可协作暂停；返回后才确认扫描暂停，避免假报“已暂停”。
                inspection.tick(cooperate=False)
                if selector.select(min(0.05, max(0, inspection.deadline - time.monotonic()))):
                    # 只需知道是否有跟踪项；不缓存 Git 的完整路径列表。
                    if os.read(process.stdout.fileno(), 1):
                        return True
                    break
            try:
                status = process.wait(timeout=max(0.01, inspection.deadline - time.monotonic()))
            except subprocess.TimeoutExpired as exc:
                raise InspectionError("Git 索引检查超时") from exc
            if status != 0:
                raise InspectionError("Git 索引检查失败")
            return False
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        if process.stdout is not None:
            process.stdout.close()
        inspection.checkpoint()


def assess_project_artifact(
    path: Path,
    project_root: Path,
    protection: Predicate,
    *,
    checkpoint: Callable[[], None] = lambda: None,
    max_entries: int = 200_000,
    timeout: float = 5.0,
) -> ArtifactAssessment:
    path, project_root = normalize_path(path), normalize_path(project_root)
    if reason := project_scope_block_reason(project_root):
        return ArtifactAssessment(reason)
    if path == project_root or not path.is_relative_to(project_root) or not artifact_name(path):
        return ArtifactAssessment("项目产物范围已不再有效")
    inspection = PathInspection(protection, checkpoint=checkpoint, max_entries=max_entries, timeout=timeout)
    try:
        if path.name in ZIG_ARTIFACTS:
            marker = inspection.checked_path(path.parent / "build.zig")
            if not stat.S_ISREG(marker.stat.st_mode):
                raise InspectionError("Zig 产物缺少普通 build.zig 文件")
        for facts in inspection.tree(path):
            name = facts.path.name.casefold()
            if name == ".git":
                return ArtifactAssessment("产物内含嵌套 Git 仓库标记，不能按可重建目录清理")
            if name.endswith("-keypair.json"):
                return ArtifactAssessment("产物内含部署密钥文件名（*-keypair.json），不能按可重建目录清理")
        # 最近的项目标记未必是仓库根；monorepo 的索引可能在更高一级。
        anchor = scan_symlink_anchor(path)
        for parent in path.parents:
            if not parent.is_relative_to(anchor):
                break
            inspection.checked_path(parent)
            marker = parent / ".git"
            try:
                inspection.probe(marker)
            except FileNotFoundError:
                continue
            directory = _git_directory(marker, inspection)
            if _tracked_content(path, parent, directory, inspection):
                return ArtifactAssessment("产物内含 Git 索引跟踪内容，不能按可重建目录清理")
            break
    except (OSError, ValueError) as exc:
        return ArtifactAssessment(f"无法完成项目产物内容检查：{exc}", complete=False)
    return ArtifactAssessment()
