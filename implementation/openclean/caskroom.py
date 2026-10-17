"""Homebrew Caskroom 旧版本目录识别与安装状态复核。"""
from __future__ import annotations

import json
import os
import re
import stat
import threading
import time
from collections import deque
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .filesystem import filesystem_id_retry, lstat_retry
from .macos import filesystem_relative_parts
from .models import FileFacts, FileIdentity, Item, ScanIssue, ScanResult, normalize_path
from .path_inspection import InspectionError, PathInspection
from .predicates import Predicate
from .processes import (
    OpenFileDetectionError,
    OpenFileSnapshot,
    ProcessDetectionError,
    ProcessSnapshot,
    capture_open_file_snapshot,
    capture_process_snapshot,
)
from .resource_walk import open_directory, open_resource
from .storage_diagnostics import _candidate_measurement
from .updater import _compare_versions

CASKROOM_SCAN_TASK = "homebrew-caskroom"
_MAX_RECEIPT_BYTES = 2 * 1024 * 1024
_MAX_TOKENS = 20_000
_MAX_METADATA_ENTRIES = 4096
_PREFIX_LINK_BUDGET = 200_000
_MAX_LINK_HOPS = 40
_TOKEN_NAME = re.compile(r"^[a-z0-9][a-z0-9@._+-]*$")
_TIMESTAMP = re.compile(r"^[0-9]{14}(?:\.[0-9]+)?$")
_LINK_DIRECTORIES = {
    "binary": "bin",
    "manpage": "share/man",
    "bash_completion": "etc/bash_completion.d",
    "zsh_completion": "share/zsh/site-functions",
    "fish_completion": "share/fish/vendor_completions.d",
    "pwsh_completion": "share/pwsh/completions",
}


def caskroom_roots() -> tuple[Path, ...]:
    roots = [Path("/opt/homebrew/Caskroom"), Path("/usr/local/Caskroom")]
    prefix = os.environ.get("HOMEBREW_PREFIX")
    if prefix and Path(prefix).is_absolute():
        roots.append(normalize_path(prefix) / "Caskroom")
    return tuple(dict.fromkeys(roots))


@dataclass(frozen=True)
class CaskVersionState:
    token: str
    receipt_version: str
    directory_version: str
    status: str
    block_reason: str = ""
    note: str = ""

    @property
    def blocks_cleanup(self) -> bool:
        return bool(self.block_reason)


@dataclass
class _LinkInventory:
    """复用共享祖先的只读检查，整批解析后复核本轮新增的对象。"""

    facts: dict[Path, FileFacts] = field(default_factory=dict)
    pending: set[Path] = field(default_factory=set)

    def probe(self, inspection: _CaskInspection, path: Path) -> FileFacts:
        if path not in self.facts:
            self.facts[path] = inspection.probe(path)
            self.pending.add(path)
        return self.facts[path]

    def verify(self, inspection: _CaskInspection) -> None:
        for path in sorted(self.pending, key=lambda value: (len(value.parts), value)):
            before = self.facts[path]
            after = inspection.probe(path)
            if (after.identity != before.identity
                    or after.stat.st_mode != before.stat.st_mode
                    or after.stat.st_mtime_ns != before.stat.st_mtime_ns
                    or after.stat.st_ctime_ns != before.stat.st_ctime_ns):
                raise InspectionError(f"Cask 链接组件在复核期间变化：{path}")
        self.pending.clear()


@dataclass
class _PrefixLinks:
    targets: dict[Path, Path | None] = field(default_factory=dict)
    scanned_prefixes: set[Path] = field(default_factory=set)
    errors: dict[Path, str] = field(default_factory=dict)
    inventory: _LinkInventory = field(default_factory=_LinkInventory)


class CaskScanCache:
    """一次扫描内共享安装检查和句柄快照；执行复核不使用此缓存。"""

    def __init__(self, protection: Predicate) -> None:
        self.protection = protection
        self._lock = threading.Lock()
        self._links = _PrefixLinks()
        self._installations: dict[Path, tuple[str, str]] = {}
        self._handles_checked = False
        self._handles: OpenFileSnapshot | None = None
        self._handles_error = ""

    @contextmanager
    def _locked(self, checkpoint: Callable[[], None]):
        # 其它扫描线程持锁时仍响应暂停与取消。
        while not self._lock.acquire(timeout=0.05):
            checkpoint()
        try:
            yield
        finally:
            self._lock.release()

    def installation_version(
        self, token_root: Path, checkpoint: Callable[[], None],
    ) -> tuple[str, str]:
        checkpoint()
        with self._locked(checkpoint):
            if token_root not in self._installations:
                self._installations[token_root] = _installation_version(
                    token_root, self.protection, checkpoint, self._links,
                )
            return self._installations[token_root]

    def open_files(self, checkpoint: Callable[[], None]) -> OpenFileSnapshot:
        checkpoint()
        with self._locked(checkpoint):
            if not self._handles_checked:
                try:
                    self._handles = capture_open_file_snapshot()
                except OpenFileDetectionError as exc:
                    self._handles_error = str(exc) or "无法获取打开文件快照"
                self._handles_checked = True
            if self._handles_error:
                raise OpenFileDetectionError(self._handles_error)
            assert self._handles is not None
            return self._handles


class _CaskInspection(PathInspection):
    def __init__(self, token_root: Path, protection: Predicate, checkpoint: Callable[[], None]):
        super().__init__(protection, checkpoint=checkpoint, max_entries=_MAX_METADATA_ENTRIES)
        self.token_root = token_root
        self.root = self.checked_path(token_root)
        if not stat.S_ISDIR(self.root.stat.st_mode) or self.root.stat.st_uid != os.getuid():
            raise InspectionError("Caskroom token 根不是当前用户的普通目录")
        caskroom = self.checked_path(token_root.parent)
        self.filesystem = filesystem_id_retry(token_root.parent)
        if (self.root.stat.st_dev != caskroom.stat.st_dev
                or filesystem_id_retry(token_root) != self.filesystem):
            raise InspectionError("Caskroom token 根跨越文件系统")
        self._verified_ancestors: set[Path] = set()

    def checked_relative(self, parts: tuple[str, ...]) -> FileFacts:
        """在 token 根内逐组件验证相对路径，避免从系统锚点重复走完全路径。"""
        current = self.token_root
        result = self.probe(current)
        for part in parts:
            current /= part
            result = self.probe(current)
            if stat.S_ISLNK(result.stat.st_mode):
                raise InspectionError("内容保护范围包含符号链接组件")
        return result

    def metadata_facts(self, path: Path) -> FileFacts:
        if not path.is_relative_to(self.token_root):
            raise InspectionError("Cask 安装证据不在 token 范围内")
        facts = self.checked_path(path)
        # 祖先的属主/设备/文件系统一致性按身份缓存，避免每次全链重查。
        for current in (path, *path.parents):
            if current == self.token_root.parent:
                break
            if current in self._verified_ancestors:
                continue
            component = facts if current == path else self.probe(current)
            if (component.stat.st_uid != self.root.stat.st_uid
                    or component.stat.st_dev != self.root.stat.st_dev
                    or filesystem_id_retry(current) != self.filesystem):
                raise InspectionError("Cask 安装证据的属主或文件系统不一致")
            self._verified_ancestors.add(current)
        return facts

    def names(self, path: Path) -> list[str]:
        facts = self.metadata_facts(path)
        with open_directory(path, self.protection) as descriptor:
            if FileIdentity.from_stat(os.fstat(descriptor)) != facts.identity:
                raise InspectionError("Cask 安装证据目录身份变化")
            with os.scandir(descriptor) as entries:
                names = []
                for entry in entries:
                    self.tick()
                    names.append(entry.name)
        return sorted(names)

    def read_json(self, path: Path) -> dict:
        facts = self.metadata_facts(path)
        if not stat.S_ISREG(facts.stat.st_mode) or facts.stat.st_size > _MAX_RECEIPT_BYTES:
            raise InspectionError("Cask 安装元数据不是普通文件或超出大小上限")
        with open_directory(path.parent, self.protection) as parent_fd:
            with open_resource(facts, parent_fd) as descriptor:
                chunks = []
                remaining = _MAX_RECEIPT_BYTES + 1
                while remaining:
                    self.tick()
                    chunk = os.read(descriptor, min(remaining, 65536))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                if not remaining:
                    raise InspectionError("Cask 安装元数据超出大小上限")
        payload = json.loads(b"".join(chunks))
        if not isinstance(payload, dict):
            raise InspectionError("Cask 安装元数据不是 JSON 对象")
        return payload


def _installed_caskfiles(inspection: _CaskInspection) -> tuple[Path, ...]:
    metadata = inspection.token_root / ".metadata"
    records: list[tuple[str, Path]] = []
    for version in inspection.names(metadata):
        path = metadata / version
        facts = inspection.metadata_facts(path)
        if not stat.S_ISDIR(facts.stat.st_mode):
            continue
        for timestamp in inspection.names(path):
            stamped = path / timestamp
            facts = inspection.metadata_facts(stamped)
            if not stat.S_ISDIR(facts.stat.st_mode) or not _TIMESTAMP.fullmatch(timestamp):
                raise InspectionError("Cask 带时间戳安装元数据的结构不明确")
            records.append((timestamp, stamped))
    if not records:
        raise InspectionError("缺少带时间戳的 Cask 安装元数据")
    newest = max(timestamp for timestamp, _ in records)
    latest = [path for timestamp, path in records if timestamp == newest]
    if len(latest) != 1:
        raise InspectionError("最新 Cask 安装元数据不唯一")
    definitions = []
    for _, stamped in sorted(records, reverse=True):
        for suffix in (".json", ".internal.json", ".rb"):
            path = stamped / "Casks" / (inspection.token_root.name + suffix)
            try:
                facts = inspection.metadata_facts(path)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(facts.stat.st_mode):
                raise InspectionError("已安装 Cask 定义不是普通文件")
            definitions.append(path)
            break
        else:
            raise InspectionError("Cask 安装元数据缺少对应定义")
    return tuple(definitions)


def _relative_artifact_path(value: object) -> Path:
    if (not isinstance(value, str) or not value or "\x00" in value
            or value.startswith("~")):
        raise InspectionError("Cask 安装入口路径无效")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise InspectionError("Cask 安装入口源路径不在当前版本内")
    return path


def _relative_link_path(value: str) -> Path:
    """保留链接原始组件；``..`` 与绝对路径因词法折叠不等于实际解析而拒绝。"""
    if not value or "\x00" in value or value.startswith("~"):
        raise InspectionError("Cask 安装链接路径无效")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise InspectionError("Cask 安装链接路径包含拒绝的组件")
    return path


def _iter_prefix_links(
    inspection: _CaskInspection, token_root: Path,
) -> list[tuple[Path, FileFacts]]:
    """有界遍历已知链接目录，包括 man 章节和嵌套 binary；不跟随链接。"""
    prefix = token_root.parent.parent
    entries: list[tuple[Path, FileFacts]] = []
    for directory in _LINK_DIRECTORIES.values():
        inspection.tick()
        root = prefix / directory
        try:
            facts = inspection.checked_path(root)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise InspectionError(f"无法检查 Cask prefix 链接目录 {root}：{exc}") from exc
        if not stat.S_ISDIR(facts.stat.st_mode):
            raise InspectionError("Cask prefix 链接目录不是普通目录")
        root_device = facts.stat.st_dev
        filesystem = filesystem_id_retry(root)
        pending = [facts]
        while pending:
            expected = pending.pop()
            current = inspection.checked_path(expected.path)
            if current.identity != expected.identity:
                raise InspectionError("Cask prefix 链接目录身份变化")
            if (current.stat.st_dev != root_device
                    or filesystem_id_retry(current.path) != filesystem):
                raise InspectionError("Cask prefix 链接目录跨越文件系统")
            with open_directory(current.path, inspection.protection) as descriptor:
                if FileIdentity.from_stat(os.fstat(descriptor)) != current.identity:
                    raise InspectionError("Cask prefix 链接目录身份变化")
                with os.scandir(descriptor) as children:
                    for child in children:
                        facts = inspection.probe(current.path / child.name)
                        if facts.identity != FileIdentity.from_stat(os.stat(
                            child.name, dir_fd=descriptor, follow_symlinks=False,
                        )):
                            raise InspectionError("Cask prefix 链接条目身份变化")
                        if stat.S_ISLNK(facts.stat.st_mode):
                            entries.append((facts.path, facts))
                        elif stat.S_ISDIR(facts.stat.st_mode):
                            pending.append(facts)
                after = inspection.probe(current.path)
                if (after.identity != current.identity
                        or after.stat.st_mtime_ns != current.stat.st_mtime_ns
                        or after.stat.st_ctime_ns != current.stat.st_ctime_ns):
                    raise InspectionError("Cask prefix 链接目录在枚举期间变化")
    return entries


def _resolve_inventory_link(
    inspection: _CaskInspection, entry: Path, inventory: _LinkInventory,
) -> Path | None:
    """只读核实残留入口的最终归属，逐组件展开链接后再处理 ``..``。"""
    current = Path(entry.anchor)
    pending = deque(entry.parts[1:])
    inventory.probe(inspection, current)
    links: list[FileFacts] = []
    visited: set[tuple[Path, tuple[str, ...]]] = set()
    while pending:
        inspection.cooperate()
        if time.monotonic() >= inspection.deadline:
            raise InspectionError("内容保护检查超出时间预算")
        part = pending.popleft()
        if part in ("", "."):
            continue
        if part == "..":
            current = current.parent
            continue
        candidate = current / part
        try:
            facts = inventory.probe(inspection, candidate)
        except FileNotFoundError:
            current = None
            break
        except OSError as exc:
            raise InspectionError(f"无法核实链接组件 {candidate}：{exc}") from exc
        if stat.S_ISLNK(facts.stat.st_mode):
            key = (candidate, tuple(pending))
            if key in visited:
                raise InspectionError(f"Cask 链接目标包含循环：{candidate}")
            if len(links) >= _MAX_LINK_HOPS:
                raise InspectionError(f"Cask 链接目标超过跳数上限：{candidate}")
            visited.add(key)
            with open_directory(current, inspection.protection) as descriptor:
                if FileIdentity.from_stat(os.stat(
                    part, dir_fd=descriptor, follow_symlinks=False,
                )) != facts.identity:
                    raise InspectionError(f"Cask 链接身份变化：{candidate}")
                raw = os.readlink(part, dir_fd=descriptor)
            after = inspection.probe(candidate)
            if (after.identity != facts.identity
                    or after.stat.st_ctime_ns != facts.stat.st_ctime_ns):
                raise InspectionError(f"Cask 链接在读取期间变化：{candidate}")
            if not raw or raw.startswith("~") or "\x00" in raw:
                raise InspectionError(f"Cask 链接目标路径无效：{candidate}")
            links.append(facts)
            if raw.startswith(os.sep):
                current = Path(os.sep)
            pending.extendleft(reversed(raw.split(os.sep)))
        else:
            if pending and not stat.S_ISDIR(facts.stat.st_mode):
                raise InspectionError(f"Cask 链接目标穿过非目录组件：{candidate}")
            current = candidate
    return current


def _resolve_link_target(
    inspection: _CaskInspection, entry: Path, token_root: Path,
    cache: dict[Path, Path | None] | None = None,
    *, allow_indirect: bool = True, inventory: _LinkInventory | None = None,
) -> Path | None:
    """残留入口有界核实最终归属；当前声明入口仍要求直接链接。"""
    if allow_indirect:
        if cache is not None and entry in cache:
            return cache[entry]
        shared_inventory = inventory if inventory is not None else _LinkInventory()
        target = _resolve_inventory_link(inspection, entry, shared_inventory)
        if inventory is None:
            shared_inventory.verify(inspection)
        if target is not None and filesystem_relative_parts(
            target, token_root.parent, probe=inspection.checked_path,
        ) is None:
            target = None
        if cache is not None:
            cache[entry] = target
        return target
    inspection.checked_path(entry.parent)
    entry_facts = inspection.probe(entry)
    if not stat.S_ISLNK(entry_facts.stat.st_mode):
        raise InspectionError("Cask 安装入口不再是符号链接")
    prefix = token_root.parent.parent
    try:
        raw = os.readlink(entry)
    except OSError as exc:
        raise InspectionError(f"无法读取 Cask 链接目标：{exc}") from exc
    if inspection.probe(entry).identity != entry_facts.identity:
        raise InspectionError("Cask 安装入口在读取期间变化")
    if raw.startswith("~") or "\x00" in raw:
        raise InspectionError("Cask 链接目标路径无效")
    parts = Path(raw)
    if parts.is_absolute():
        # 绝对目标逐组件重新验证，从根目录开始定位。
        resolved = _walk_components(
            inspection, [p for p in parts.parts if p != os.sep], [], Path(os.sep), token_root,
        )
    else:
        # 绝对 target 可以把入口放在 prefix 外；其相对 readlink 从入口父目录解析。
        if not entry.parent.is_relative_to(prefix):
            prefix = Path(os.sep)
        resolved = _walk_components(
            inspection, list(parts.parts), list(entry.parent.relative_to(prefix).parts), prefix, token_root,
        )
    if resolved is None:
        if cache is not None:
            cache[entry] = None
        return None
    if parts.is_absolute():
        target = Path(os.sep).joinpath(*resolved) if resolved else Path(os.sep)
    else:
        target = prefix.joinpath(*resolved) if resolved else prefix
    try:
        inspection.checked_path(target.parent)
        target_facts = inspection.probe(target)
    except FileNotFoundError:
        target = None
    else:
        if stat.S_ISLNK(target_facts.stat.st_mode):
            raise InspectionError("Cask 安装入口目标仍是符号链接，仅支持直接链接")
        elif filesystem_relative_parts(
            target, token_root.parent, probe=inspection.checked_path,
        ) is None:
            target = None
    if inspection.probe(entry).identity != entry_facts.identity:
        raise InspectionError("Cask 安装入口在复核期间变化")
    if cache is not None:
        cache[entry] = target
    return target


def _walk_components(
    inspection: _CaskInspection, components: list[str], base: list[str], prefix: Path,
    token_root: Path,
) -> list[str] | None:
    resolved = list(base)
    for index, part in enumerate(components):
        if part in (os.sep, "/", "."):
            continue
        if part == "..":
            if not resolved:
                raise InspectionError("Cask 链接目标回退到允许范围之外")
            resolved.pop()
            continue
        resolved.append(part)
        if index < len(components) - 1:
            candidate = prefix.joinpath(*resolved)
            try:
                if candidate.is_relative_to(token_root):
                    facts = inspection.checked_relative(
                        candidate.relative_to(token_root).parts,
                    )
                else:
                    facts = inspection.checked_path(candidate)
            except FileNotFoundError as exc:
                raise InspectionError("Cask 链接目标组件不存在") from exc
            if not stat.S_ISDIR(facts.stat.st_mode):
                raise InspectionError("Cask 链接目标穿过非目录组件")
    return resolved


def _scan_prefix_links(
    inspection: _CaskInspection, token_root: Path, current: Path,
    prefix_links: _PrefixLinks,
) -> None:
    """本 token 范围内的 prefix 链接目标不得指向当前版本之外。

    ``prefix_links`` 在一次扫描内复用目录枚举和链接解析；
    链接归属按目标所在的 token 判定：指向其它 token 的链接由那个 token
    自己的扫描复核，不由当前 token 的判断覆盖。
    """
    prefix = token_root.parent.parent
    if prefix in prefix_links.errors:
        raise InspectionError(prefix_links.errors[prefix])
    if prefix not in prefix_links.scanned_prefixes:
        try:
            for entry, _facts in _iter_prefix_links(inspection, token_root):
                inspection.tick()
                try:
                    _resolve_link_target(
                        inspection, entry, token_root, prefix_links.targets,
                        inventory=prefix_links.inventory,
                    )
                except OSError as exc:
                    raise InspectionError(f"无法核实 Cask prefix 入口 {entry}：{exc}") from exc
            prefix_links.inventory.verify(inspection)
        except OSError as exc:
            # 同一轮中共享无法完整读取的结论，避免逐 token 重复耗尽遍历预算。
            prefix_links.errors[prefix] = str(exc)
            raise
        prefix_links.scanned_prefixes.add(prefix)
    prefix_links.inventory.verify(inspection)
    for entry, target in prefix_links.targets.items():
        inspection.tick()
        if target is None or filesystem_relative_parts(
            target, token_root, probe=inspection.checked_path,
        ) is None:
            # 目标属于其它 token 或 prefix 外，由其归属 token 的扫描判定。
            continue
        if filesystem_relative_parts(target, current, probe=inspection.checked_path) is None:
            raise InspectionError(f"Cask 安装入口仍指向旧版本或其它位置：{entry}")


def _check_historical_links(
    inspection: _CaskInspection, definitions: tuple[Path, ...], links: _PrefixLinks,
) -> dict[Path, dict]:
    """按历史 JSON 的安装目标检查 prefix 外入口；缺失入口视为已经移除。"""
    payloads = {}
    prefix = inspection.token_root.parent.parent
    for path in definitions:
        if path.suffix != ".json":
            raise InspectionError("历史 Cask Ruby 定义的安装入口无法核实，仅报告")
        definition = inspection.read_json(path)
        payloads[path] = definition
        if definition.get("version") != path.parent.parent.parent.name:
            raise InspectionError("历史 Cask 定义与安装元数据的版本不一致")
        artifacts = definition.get("artifacts")
        if not isinstance(artifacts, list):
            raise InspectionError("历史 Cask 定义缺少安装入口记录")
        for artifact in artifacts:
            inspection.tick()
            if not isinstance(artifact, dict) or len(artifact) != 1:
                raise InspectionError("历史 Cask 安装入口结构无法核实")
            kind, arguments = next(iter(artifact.items()))
            if kind == "zap":
                continue
            if kind == "generate_completions_from_executable":
                _, targets = _generated_completion_paths(prefix, arguments)
            else:
                if kind not in _LINK_DIRECTORIES or not isinstance(arguments, list) or not 1 <= len(arguments) <= 2:
                    raise InspectionError("历史 Cask 包含无法核实的安装入口，仅报告")
                source = _relative_artifact_path(arguments[0])
                targets = (_artifact_target(prefix, kind, arguments, source),)
            for target in targets:
                try:
                    inspection.checked_path(target.parent)
                    facts = inspection.probe(target)
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(facts.stat.st_mode):
                    _resolve_link_target(
                        inspection, target, inspection.token_root, links.targets,
                        inventory=links.inventory,
                    )
    return payloads


def _artifact_target(prefix: Path, kind: str, arguments: list, source: Path) -> Path:
    destination = source.name
    if len(arguments) == 2:
        options = arguments[1]
        if not isinstance(options, dict) or set(options) != {"target"}:
            raise InspectionError("Cask 安装入口 target 参数无效")
        destination = options["target"]
    if not isinstance(destination, str) or not destination or "\x00" in destination:
        raise InspectionError("Cask 安装入口 target 路径无效")
    if kind == "binary":
        path = Path(destination)
        if path.is_absolute():
            return normalize_path(path)
        return prefix / "bin" / _relative_artifact_path(destination)
    if len(arguments) != 1:
        raise InspectionError("仅核实 manpage/completion 的默认目标名称")
    name = source.name
    directory = prefix / _LINK_DIRECTORIES[kind]
    if kind == "manpage":
        match = re.search(r"\.([1-8nl])(?:\.gz)?$", name)
        if match is None:
            raise InspectionError("Cask manpage 缺少有效章节后缀")
        directory /= "man" + match[1]
    elif kind == "bash_completion":
        name = Path(name).stem
    elif kind == "zsh_completion" and not name.startswith("_"):
        name = "_" + Path(name).stem
    elif kind == "fish_completion" and not name.endswith(".fish"):
        name = Path(name).stem + ".fish"
    elif kind == "pwsh_completion" and not (name.startswith("_") and name.endswith(".ps1")):
        name = "_" + Path(name).stem + ".ps1"
    return directory / name


def _generated_completion_paths(prefix: Path, arguments: object) -> tuple[Path, tuple[Path, ...]]:
    if not isinstance(arguments, list) or len(arguments) < 2 or not isinstance(arguments[-1], dict):
        raise InspectionError("Cask 生成式补全参数无法核实")
    options = arguments[-1]
    if set(options) - {"base_name", "shell_parameter_format", "shells"}:
        raise InspectionError("Cask 生成式补全包含未知选项")
    if not all(isinstance(value, str) for value in arguments[:-1]):
        raise InspectionError("Cask 生成式补全命令参数无效")
    source = _relative_artifact_path(arguments[0])
    shells = options.get("shells")
    if (not isinstance(shells, list) or not shells
            or any(shell not in ("bash", "zsh", "fish", "pwsh") for shell in shells)):
        raise InspectionError("Cask 生成式补全的 shell 范围不明确")
    name = options.get("base_name") or source.name
    name_path = _relative_artifact_path(name)
    if len(name_path.parts) != 1:
        raise InspectionError("Cask 生成式补全名称不是单个文件名")
    targets = tuple(_artifact_target(prefix, shell + "_completion", [name], Path(name)) for shell in shells)
    return source, targets


def _check_generated_completions(
    inspection: _CaskInspection, source_root: Path, arguments: object,
) -> None:
    source, targets = _generated_completion_paths(inspection.token_root.parent.parent, arguments)
    if not stat.S_ISREG(inspection.metadata_facts(source_root / source).stat.st_mode):
        raise InspectionError("Cask 生成式补全源不是当前版本普通文件")
    for target in targets:
        facts = inspection.checked_path(target)
        if not stat.S_ISREG(facts.stat.st_mode):
            raise InspectionError("Cask 生成式补全目标不是独立普通文件")


def _check_installed_links(
    inspection: _CaskInspection, current: Path, receipt: dict, definition: dict,
    prefix_links: _PrefixLinks,
) -> None:
    if receipt.get("uninstall_flight_blocks"):
        raise InspectionError("Cask 包含卸载脚本钩子，仅报告")
    artifacts = definition.get("artifacts", receipt.get("uninstall_artifacts"))
    if ("artifacts" in definition and "uninstall_artifacts" in receipt
            and definition["artifacts"] != receipt["uninstall_artifacts"]):
        raise InspectionError("Cask 定义与回执的安装入口不一致")
    if not isinstance(artifacts, list) or not artifacts:
        raise InspectionError("缺少可核实的 Cask 安装入口")
    specs = definition.get("url_specs", {})
    if not isinstance(specs, dict):
        raise InspectionError("Cask 源路径元数据无效")
    source_root = current
    if specs.get("only_path"):
        source_root /= _relative_artifact_path(specs["only_path"])
    installed_links = 0
    for artifact in artifacts:
        inspection.tick()
        if not isinstance(artifact, dict) or len(artifact) != 1:
            raise InspectionError("Cask 安装入口结构无法核实")
        kind, arguments = next(iter(artifact.items()))
        if kind == "zap":
            # zap 只描述显式卸载时的用户数据清理，不是已安装入口。
            continue
        if kind == "generate_completions_from_executable":
            _check_generated_completions(inspection, source_root, arguments)
            continue
        if kind not in _LINK_DIRECTORIES:
            raise InspectionError(f"尚不能核实 Cask {kind} 安装入口，仅报告")
        if not isinstance(arguments, list) or not 1 <= len(arguments) <= 2:
            raise InspectionError("Cask 安装入口参数无法核实")
        source = source_root / _relative_artifact_path(arguments[0])
        source_facts = inspection.metadata_facts(source)
        if not stat.S_ISREG(source_facts.stat.st_mode):
            raise InspectionError("Cask 当前安装入口源不是普通文件")
        target = _artifact_target(inspection.token_root.parent.parent, kind, arguments, source)
        inspection.checked_path(target.parent)
        target_facts = inspection.probe(target)
        if not stat.S_ISLNK(target_facts.stat.st_mode):
            raise InspectionError("Cask 安装入口不是可核实的符号链接")
        with open_directory(target.parent, inspection.protection) as parent_fd:
            if FileIdentity.from_stat(os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)) != target_facts.identity:
                raise InspectionError("Cask 安装入口身份变化")
            linked = _resolve_link_target(
                inspection, target, inspection.token_root, allow_indirect=False,
            )
            prefix_links.targets[target] = linked
        if linked is None or filesystem_relative_parts(linked, current, probe=inspection.checked_path) is None:
            raise InspectionError("Cask 安装入口仍指向旧版本或其它位置")
        linked_facts = inspection.metadata_facts(linked)
        if linked_facts.identity != source_facts.identity:
            raise InspectionError("Cask 安装入口与当前版本源文件不一致")
        if inspection.probe(target).identity != target_facts.identity:
            raise InspectionError("Cask 安装入口在复核期间变化")
        installed_links += 1
    if not installed_links:
        raise InspectionError("缺少可核实的 Cask 安装链接")


def _installation_version(
    token_root: Path, protection: Predicate, checkpoint: Callable[[], None] = lambda: None,
    prefix_links: _PrefixLinks | None = None,
) -> tuple[str, str]:
    try:
        inspection = _CaskInspection(token_root, protection, checkpoint)
        # prefix 链接解析（首次填充或独立复核）有独立预算，
        # 不与 token 自身的元数据检查共用同一配额。
        inspection.remaining += _PREFIX_LINK_BUDGET
        receipt = inspection.read_json(token_root / ".metadata/INSTALL_RECEIPT.json")
        source = receipt.get("source")
        version = source.get("version") if isinstance(source, dict) else None
        if not isinstance(version, str) or not version or len(version) > 256:
            raise InspectionError("安装回执缺少有效的 source.version")
        if _compare_versions(version, version) is None:
            raise InspectionError("回执版本不是可比较的纯数字版本号")
        caskfiles = _installed_caskfiles(inspection)
        caskfile = caskfiles[0]
        if caskfile.parent.parent.parent.name != version:
            raise InspectionError("安装回执与最新安装元数据的版本不一致")
        current = token_root / version
        current_facts = inspection.metadata_facts(current)
        if not stat.S_ISDIR(current_facts.stat.st_mode):
            raise InspectionError("回执版本对应的当前目录不是普通目录")
        # Ruby 定义仅核对路径与文件身份，绝不执行或解析其中的代码。
        definition = (inspection.read_json(caskfile)
                      if caskfile.suffix == ".json" else {})
        if definition.get("version", version) != version:
            raise InspectionError("已安装 Cask 定义与回执版本不一致")
        # 扫描共用缓存；每次执行复核都从新的缓存开始。
        links = _PrefixLinks() if prefix_links is None else prefix_links
        _check_installed_links(inspection, current, receipt, definition, links)
        historical = _check_historical_links(inspection, caskfiles[1:], links)
        # 除声明的入口外，prefix 链接目录中的任何残留链接也不得指向当前版本之外；
        # 历史版本记录过的入口在升级改名后仍可能保留在 bin/manpage 等目录中。
        _scan_prefix_links(inspection, token_root, current, links)
        if inspection.metadata_facts(token_root).identity != inspection.root.identity:
            raise InspectionError("Caskroom token 根在复核期间变化")
        if (inspection.read_json(token_root / ".metadata/INSTALL_RECEIPT.json") != receipt
                or _installed_caskfiles(inspection) != caskfiles
                or inspection.metadata_facts(current).identity != current_facts.identity
                or any(inspection.read_json(path) != value for path, value in historical.items())
                or (caskfile.suffix == ".json" and inspection.read_json(caskfile) != definition)):
            raise InspectionError("Cask 安装证据在复核期间变化")
        return version, ""
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        return "", f"无法核实 Cask 安装状态：{exc}"


def assess_cask_version(token: str, receipt_version: str, directory_version: str) -> CaskVersionState:
    comparison = _compare_versions(directory_version, receipt_version)
    if comparison is None:
        return CaskVersionState(
            token, receipt_version, directory_version, "version_unknown",
            "目录或回执版本不是纯数字版本号，无法比较",
        )
    if comparison > 0:
        return CaskVersionState(
            token, receipt_version, directory_version, "pending_update",
            "目录版本高于回执版本，可能正在升级",
        )
    if comparison == 0:
        return CaskVersionState(
            token, receipt_version, directory_version, "same_version_residue",
            "目录版本与回执一致，是当前安装版本",
        )
    return CaskVersionState(
        token, receipt_version, directory_version, "older_version_residue",
        note=f"目录 {directory_version} 低于已核实安装版本 {receipt_version}",
    )


def assess_cask_path(
    path: Path, protection: Predicate, *, checkpoint: Callable[[], None] = lambda: None,
    scan_cache: CaskScanCache | None = None,
) -> CaskVersionState:
    version, error = (scan_cache.installation_version(path.parent, checkpoint)
                      if scan_cache is not None else
                      _installation_version(path.parent, protection, checkpoint))
    if error:
        return CaskVersionState(path.parent.name, "", path.name, "version_unknown", error)
    return assess_cask_version(path.parent.name, version, path.name)


def scan_caskroom_versions(
    protection: Predicate,
    *,
    roots: tuple[Path, ...] | None = None,
    snapshots: tuple[ProcessSnapshot | None, OpenFileSnapshot | None] | None = None,
    now: float | None = None,
    checkpoint: Callable[[], None] = lambda: None,
    scan_cache: CaskScanCache | None = None,
) -> ScanResult:
    from .engine import Cancelled
    from .homebrew import running

    result = ScanResult()
    scan_cache = scan_cache if scan_cache is not None else CaskScanCache(protection)
    if snapshots is None:
        try:
            processes: ProcessSnapshot | None = capture_process_snapshot()
        except ProcessDetectionError:
            processes = None
            result.issues.append(ScanIssue(
                code="process_detection_failed", message="无法获取进程快照；相关候选保持不可执行",
                task=CASKROOM_SCAN_TASK, blocking=False,
            ))
        try:
            handles: OpenFileSnapshot | None = scan_cache.open_files(checkpoint)
        except Cancelled:
            result.cancelled = True
            return result
        except OpenFileDetectionError:
            handles = None
            result.issues.append(ScanIssue(
                code="open_file_detection_failed", message="无法获取打开文件快照；相关候选保持不可执行",
                task=CASKROOM_SCAN_TASK, blocking=False,
            ))
    else:
        processes, handles = snapshots
    brew_running = processes is not None and running(processes)
    observed_at = time.time() if now is None else now
    scanned_tokens = 0
    try:
        for raw_root in dict.fromkeys(roots if roots is not None else caskroom_roots()):
            root = normalize_path(raw_root)
            checkpoint()
            try:
                inspection = PathInspection(protection, checkpoint=checkpoint, max_entries=_MAX_TOKENS + 64)
                facts = inspection.checked_path(root)
                with open_directory(root, protection) as descriptor:
                    with os.scandir(descriptor) as entries:
                        tokens = []
                        for entry in entries:
                            inspection.tick()
                            if _TOKEN_NAME.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False):
                                tokens.append(entry.name)
                for token in sorted(tokens):
                    checkpoint()
                    if scanned_tokens >= _MAX_TOKENS:
                        raise InspectionError("Caskroom token 数超过上限")
                    scanned_tokens += 1
                    _scan_token(root / token, facts.stat.st_dev, protection, result,
                                brew_running=brew_running, now=observed_at,
                                process_snapshot=processes, open_files=handles,
                                checkpoint=checkpoint, scan_cache=scan_cache)
            except FileNotFoundError:
                continue
            except OSError as exc:
                # 用户显式忽略或保护整个扫描根时静默跳过，不当作文件系统故障。
                if isinstance(exc, InspectionError) and ("忽略" in str(exc) or "云占位" in str(exc)):
                    continue
                result.issues.append(_fs_issue(exc, root))
    except Cancelled:
        result.cancelled = True
    return result


def _scan_token(
    token_root: Path,
    root_device: int,
    protection: Predicate,
    result: ScanResult,
    *,
    brew_running: bool,
    now: float,
    process_snapshot: ProcessSnapshot | None,
    open_files: OpenFileSnapshot | None,
    checkpoint: Callable[[], None],
    scan_cache: CaskScanCache,
) -> None:
    task = CASKROOM_SCAN_TASK
    try:
        inspection = _CaskInspection(token_root, protection, checkpoint)
        versions = []
        for name in inspection.names(token_root):
            if name.startswith("."):
                continue
            path = token_root / name
            try:
                facts = inspection.metadata_facts(path)
            except OSError as exc:
                result.issues.append(_fs_issue(exc, path))
                continue
            if stat.S_ISDIR(facts.stat.st_mode):
                versions.append(name)
    except OSError as exc:
        result.issues.append(_fs_issue(exc, token_root))
        return
    if len(versions) <= 1:
        return
    receipt_version, receipt_error = scan_cache.installation_version(token_root, checkpoint)
    for directory_version in versions:
        checkpoint()
        state = (
            CaskVersionState(token_root.name, "", directory_version, "version_unknown", receipt_error)
            if receipt_error else assess_cask_version(token_root.name, receipt_version, directory_version)
        )
        path = token_root / directory_version
        try:
            before = lstat_retry(path)
        except OSError as exc:
            result.issues.append(_fs_issue(exc, path))
            continue
        issue_start = len(result.issues)
        measurement = _candidate_measurement(
            path, root_device, protection, result.issues, task=task, now=now,
            checkpoint=checkpoint,
        )
        if measurement is None:
            continue
        try:
            after = lstat_retry(path)
            token_after = inspection.metadata_facts(token_root)
        except OSError as exc:
            result.issues.append(_fs_issue(exc, path))
            continue
        if (before.st_dev, before.st_ino, before.st_uid, before.st_mode,
                before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_dev, after.st_ino, after.st_uid, after.st_mode,
                after.st_mtime_ns, after.st_ctime_ns) or token_after.identity != inspection.root.identity:
            result.issues.append(ScanIssue(
                code="target_changed_during_scan", message="目标在计量期间变化",
                task=task, path=path, blocking=True,
            ))
            continue
        open_handles = open_files.count_under(path) if open_files is not None else None
        newest_mtime = measurement.newest_mtime or None
        age_days = max(0, int((now - newest_mtime) // 86400)) if newest_mtime is not None else None
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
        notes = [f"Homebrew cask {token_root.name} 版本目录", state.note]
        if state.status == "older_version_residue":
            notes.append("仅移动已核实的旧版本目录到同卷 Trash；保留 token 和 .metadata")
        result.items.append(Item(
            path, measurement.allocated_bytes, "Homebrew Cask 旧版本", "critical",
            "；".join(part for part in notes if part),
            logical_size=measurement.logical_bytes,
            allocated_size=measurement.allocated_bytes,
            actionable=actionable,
            action_block_reason="；".join(block_reasons),
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
            cleanup_root_identity=inspection.root.identity,
            updater_status=state.status,
            installed_version=state.receipt_version,
            staged_version=state.directory_version,
        ))


def _fs_issue(exc: OSError, path: Path) -> ScanIssue:
    return ScanIssue(
        code="permission_denied" if isinstance(exc, PermissionError) else "filesystem_error",
        message=str(exc), task=CASKROOM_SCAN_TASK, path=path,
    )
