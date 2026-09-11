"""扫描与执行共用的应用归属和 updater 范围判定。"""
from __future__ import annotations

import stat
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from .application_ownership import (
    APPLICATION_PATH_RULES,
    DARWIN_CACHE_PROCESS_MARKERS,
    ApplicationResolver,
)
from .filesystem import lstat_retry, scandir_entries
from .macos import scan_symlink_anchor
from .models import FileFacts, normalize_path
from .predicates import Predicate, ProtectionGate
from .processes import ProcessDetectionError, ProcessSnapshot
from .scanpoints import DOMAINS
from .updater import UPDATER_RULES, UpdaterAssessment, assess_updater_candidate


class GuardInspectionError(OSError):
    pass


@dataclass(frozen=True)
class UpdaterScope:
    root: Path
    # None 表示已知 updater 根当前没有暂存对象，不表示该范围无需实时检查。
    assessment: UpdaterAssessment | None


@dataclass(frozen=True)
class CleanupGuards:
    process_markers: tuple[str, ...] = ()
    updater_scopes: tuple[UpdaterScope, ...] = ()
    block_reason: str = ""
    note: str = ""
    process_error: str = ""
    resource_in_use: bool = False

    @property
    def updater(self) -> UpdaterAssessment | None:
        return next((scope.assessment for scope in self.updater_scopes
                     if scope.assessment is not None), None)


class CleanupGuardContext:
    """一次扫描/预检共享有界归属查询和按需进程快照；实时复核使用新实例。"""

    def __init__(
        self,
        protection: Predicate,
        capture_processes: Callable[[], ProcessSnapshot],
        *,
        home: Path | None = None,
        application_resolver: ApplicationResolver | None = None,
    ) -> None:
        self.home = normalize_path(home or Path.home())
        self.protection = protection
        self.resolver = application_resolver or ApplicationResolver(home=self.home)
        self.capture_processes = capture_processes
        self._process_lock = threading.Lock()
        self._process_checked = False
        self._snapshot: ProcessSnapshot | None = None
        self._process_error = ""

    def _probe(self, path: Path) -> FileFacts | None:
        anchor = scan_symlink_anchor(path, home=self.home)
        current = anchor
        parts = ("", *path.relative_to(anchor).parts)
        for index, part in enumerate(parts):
            if part:
                current /= part
            if isinstance(self.protection, ProtectionGate) and self.protection.knowledge_base_ignores(current):
                raise GuardInspectionError("应用保护范围命中忽略或保护规则")
            try:
                facts = FileFacts(current, lstat_retry(current))
            except FileNotFoundError:
                return None
            if (stat.S_ISLNK(facts.stat.st_mode) or facts.is_probable_cloud_placeholder
                    or self.protection.should_ignore(facts)):
                raise GuardInspectionError("应用保护范围包含链接、云占位或忽略/保护路径")
            if index < len(parts) - 1 and not stat.S_ISDIR(facts.stat.st_mode):
                return None
        return facts

    def _overlaps(self, path: Path, root: Path) -> bool:
        if path.is_relative_to(root):
            return True
        return root.is_relative_to(path) and self._probe(root) is not None

    def _registered_roots(self, darwin_cache_root: Path | None) -> Iterator[tuple[Path, tuple[str, ...]]]:
        for rule in APPLICATION_PATH_RULES:
            yield self.home / rule.relative_path, rule.process_markers
        for points in DOMAINS.values():
            for point in points:
                if point.scanner or not point.running_process_markers:
                    continue
                for raw in point.paths:
                    root = self.home / raw[2:] if raw.startswith("~/") else normalize_path(raw)
                    yield root, point.running_process_markers
        if darwin_cache_root is not None:
            for name, markers in DARWIN_CACHE_PROCESS_MARKERS.items():
                yield darwin_cache_root / name, markers

    def _dynamic_roots(self, path: Path, darwin_cache_root: Path | None) -> Iterator[Path]:
        cache_roots = [self.home / "Library/Caches"]
        if darwin_cache_root is not None:
            cache_roots.append(darwin_cache_root)
        for root in dict.fromkeys(cache_roots):
            if path != root and path.is_relative_to(root):
                yield root / path.relative_to(root).parts[0]
            elif root.is_relative_to(path):
                facts = self._probe(root)
                if facts is None or not stat.S_ISDIR(facts.stat.st_mode):
                    continue
                # 仅枚举公开 cache 根的一级名称，不遍历 HOME 或缓存内容。
                yield from sorted(Path(entry.path) for entry in scandir_entries(root))

    def _processes(self) -> tuple[ProcessSnapshot | None, str]:
        with self._process_lock:
            if not self._process_checked:
                try:
                    self._snapshot = self.capture_processes()
                except ProcessDetectionError as exc:
                    self._process_error = str(exc) or "无法获取进程快照"
                self._process_checked = True
            return self._snapshot, self._process_error

    def assess(
        self,
        path: Path,
        *,
        process_markers: tuple[str, ...] = (),
        darwin_cache_root: Path | None = None,
    ) -> CleanupGuards:
        path = normalize_path(path)
        markers = list(process_markers)
        notes: list[str] = []
        scopes: list[UpdaterScope] = []
        reason = ""
        try:
            if self._probe(path) is None:
                raise GuardInspectionError("应用保护范围已不存在，需重新扫描")
            roots = tuple(self._registered_roots(darwin_cache_root))
            for root, owned_markers in roots:
                if self._overlaps(path, root):
                    markers.extend(owned_markers)
            for root in self._dynamic_roots(path, darwin_cache_root):
                if any(root.is_relative_to(known) for known, _ in roots):
                    continue
                if self._probe(root) is None:
                    continue
                resolution = self.resolver.resolve(root, darwin_cache_root=darwin_cache_root)
                markers.extend(resolution.process_markers)
                if resolution.note:
                    notes.append(resolution.note)
            for rule in UPDATER_RULES:
                root = self.home / rule.relative_root
                if not self._overlaps(path, root):
                    continue
                updater = assess_updater_candidate(root, home=self.home, path_probe=self._probe)
                scopes.append(UpdaterScope(root, updater))
                if updater is None:
                    continue
                notes.append(updater.note)
                if path != root:
                    reason = "选择范围覆盖 updater 暂存区，需重新扫描并精确审阅 updater 根目录"
                elif updater.blocks_cleanup:
                    reason = updater.block_reason
        except OSError as exc:
            reason = f"无法完成应用保护范围复核：{exc}"
        markers = tuple(dict.fromkeys(markers))
        process_error = ""
        running = False
        if markers:
            snapshot, process_error = self._processes()
            running = snapshot is not None and snapshot.any_running(markers)
            if snapshot is None:
                reason = reason or "无法确认相关应用是否正在运行"
            elif running:
                reason = reason or "相关应用正在运行"
        return CleanupGuards(markers, tuple(scopes), reason,
                             "；".join(dict.fromkeys(notes)), process_error, running)
