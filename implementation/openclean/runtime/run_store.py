"""Bounded, atomic Run bundles; all reads bind one Run to one Finding manifest.

Schema 2 stores HOME and strategy versions. Older bundles require a new inspect;
there is intentionally no migration that invents missing execution evidence.
"""
from __future__ import annotations

import fcntl
import json
import os
import secrets
import stat
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..core.errors import FindingNotInRunError, RunExpiredError, RunNotFoundError, RunStoreError
from ..core.identifiers import valid_id
from ..core.models import ID_PREFIX_RUN, RUN_TTL_SECONDS, Finding, Run
from ..core.serialization import decode_dataclass, reject_json_constant, strict_json_pairs
from ..models import normalize_path
from ..macos import scan_symlink_anchor
from .bundle_validation import validate_bundle

BUNDLE_SCHEMA_VERSION = 2
MAX_RUNS = 64
MAX_RUN_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024


def default_run_store_dir() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg and Path(xdg).is_absolute():
        base = Path(xdg)
    else:
        base = Path.home() / ".local/state"
    return normalize_path(base / "openclean/runs")


def finding_to_dict(finding: Finding) -> dict[str, Any]:
    return asdict(finding)


def finding_from_dict(data: dict[str, Any]) -> Finding:
    return decode_dataclass(Finding, data, "finding", require_all=True)


def run_to_dict(run: Run) -> dict[str, Any]:
    return asdict(run)


def run_from_dict(data: dict[str, Any]) -> Run:
    return decode_dataclass(Run, data, "run", require_all=True)


class RunStore:
    """Private, owner-only bundles, oldest-written eviction (not read-updated LRU)."""

    def __init__(self, directory: str | os.PathLike[str] | None = None) -> None:
        self.directory = normalize_path(directory or default_run_store_dir())

    def _run_path(self, run_id: str) -> Path:
        if not valid_id(run_id, ID_PREFIX_RUN):
            raise RunStoreError(f"非法 run_id：{run_id!r}")
        return self.directory / f"{run_id[len(ID_PREFIX_RUN):]}.json"

    @staticmethod
    def _private(st: os.stat_result, *, directory: bool) -> None:
        expected = 0o700 if directory else 0o600
        correct_type = stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode)
        if not correct_type or st.st_uid != os.getuid():
            raise RunStoreError("Run Store 对象类型或所有者不符")
        if stat.S_IMODE(st.st_mode) != expected:
            raise RunStoreError(f"Run Store 模式必须为 {oct(expected)}")
        if not directory and st.st_nlink != 1:
            raise RunStoreError("Run 文件不得有硬链接")

    @contextmanager
    def _directory_fd(self, *, create: bool = False) -> Iterator[int]:
        fd = -1
        try:
            anchor = scan_symlink_anchor(self.directory)
            # Walk relative to pinned directory descriptors. Ancestor replacement
            # cannot redirect a later open or mkdir into a symlink destination.
            fd = os.open(anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            for part in self.directory.relative_to(anchor).parts:
                if create:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            self._private(os.fstat(fd), directory=True)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield fd
        except RunStoreError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise RunStoreError(f"Run Store 读写失败：{exc}") from exc
        finally:
            if fd >= 0:
                os.close(fd)

    @staticmethod
    def _decode(raw: bytes, expected_id: str) -> tuple[Run, tuple[Finding, ...]]:
        try:
            payload = json.loads(raw, object_pairs_hook=strict_json_pairs,
                                 parse_constant=reject_json_constant)
            if not isinstance(payload, dict) or set(payload) != {"schema_version", "run", "findings"}:
                raise ValueError("Run bundle 字段不完整；请重新 inspect")
            if type(payload["schema_version"]) is not int or payload["schema_version"] != BUNDLE_SCHEMA_VERSION:
                raise ValueError("Run bundle 版本不支持；请重新 inspect")
            if not isinstance(payload["findings"], list):
                raise ValueError("findings 必须是数组")
            run = run_from_dict(payload["run"])
            findings = tuple(finding_from_dict(f) for f in payload["findings"])
            if run.run_id != expected_id:
                raise ValueError("Run ID 与文件名不一致")
            validate_bundle(run, findings)
            return run, findings
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError) as exc:
            raise RunStoreError(f"Run bundle 内容无效：{exc}") from exc

    def _read_at(self, fd: int, name: str, run_id: str) -> tuple[Run, tuple[Finding, ...]]:
        try:
            file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        except FileNotFoundError:
            raise RunNotFoundError(f"Run 不存在：{run_id}") from None
        try:
            facts = os.fstat(file_fd)
            self._private(facts, directory=False)
            if facts.st_size > MAX_RUN_BYTES:
                raise RunStoreError("Run 文件超过单文件容量")
            with os.fdopen(file_fd, "rb", closefd=False) as stream:
                raw = stream.read(MAX_RUN_BYTES + 1)
            if len(raw) > MAX_RUN_BYTES:
                raise RunStoreError("Run 文件超过单文件容量")
            return self._decode(raw, run_id)
        finally:
            os.close(file_fd)

    def save(self, run: Run, findings: Sequence[Finding]) -> None:
        name = self._run_path(run.run_id).name
        try:
            validate_bundle(run, findings)
            raw = (json.dumps({"schema_version": BUNDLE_SCHEMA_VERSION,
                               "run": run_to_dict(run),
                               "findings": [finding_to_dict(f) for f in findings]},
                              ensure_ascii=False, separators=(",", ":"),
                              allow_nan=False) + "\n").encode("utf-8")
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError) as exc:
            raise RunStoreError(f"无法保存不一致 Run：{exc}") from exc
        if len(raw) > min(MAX_RUN_BYTES, MAX_TOTAL_BYTES) or MAX_RUNS < 1:
            raise RunStoreError("Run 超过存储容量，未写入")
        with self._directory_fd(create=True) as fd:
            try:
                os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise RunStoreError("Run ID 已存在；不覆盖已固化的 Run")
            temporary = f".{name}.{secrets.token_hex(12)}.tmp"
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=fd)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
                os.fsync(fd)
                self._enforce_capacity(fd, keep=name)
            finally:
                try:
                    os.unlink(temporary, dir_fd=fd)
                except FileNotFoundError:
                    pass

    def _entries(self, fd: int) -> list[tuple[str, os.stat_result]]:
        entries = []
        for name in os.listdir(fd):
            if not name.endswith(".json") or not valid_id("run:" + name[:-5], ID_PREFIX_RUN):
                continue
            try:
                facts = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISREG(facts.st_mode):
                entries.append((name, facts))
        return entries

    def _enforce_capacity(self, fd: int, *, keep: str) -> None:
        entries = self._entries(fd)
        total = sum(s.st_size for _, s in entries)
        count = len(entries)
        for name, facts in sorted(entries, key=lambda x: (x[1].st_mtime_ns, x[0])):
            if count <= MAX_RUNS and total <= MAX_TOTAL_BYTES:
                break
            if name == keep:
                continue
            os.unlink(name, dir_fd=fd)
            count -= 1
            total -= facts.st_size
        os.fsync(fd)

    def load_bundle(self, run_id: str, *, now: float | None = None) -> tuple[Run, tuple[Finding, ...]]:
        name = self._run_path(run_id).name
        now = time.time() if now is None else now
        if not self.directory.exists() and not self.directory.is_symlink():
            raise RunNotFoundError(f"Run 不存在：{run_id}")
        with self._directory_fd() as fd:
            run, findings = self._read_at(fd, name, run_id)
            if run.expired(now):
                os.unlink(name, dir_fd=fd)
                os.fsync(fd)
                raise RunExpiredError(f"Run 已过期：{run_id}")
            return run, findings

    def load_run(self, run_id: str, *, now: float | None = None) -> Run:
        return self.load_bundle(run_id, now=now)[0]

    def load_findings(self, run_id: str, *, now: float | None = None) -> tuple[Finding, ...]:
        return self.load_bundle(run_id, now=now)[1]

    def load_finding(self, run_id: str, finding_id: str, *, now: float | None = None) -> Finding:
        _, findings = self.load_bundle(run_id, now=now)
        for finding in findings:
            if finding.finding_id == finding_id:
                return finding
        raise FindingNotInRunError(f"Finding {finding_id} 不属于 Run {run_id}")

    def prune_expired(self, *, now: float | None = None) -> int:
        if not self.directory.exists() and not self.directory.is_symlink():
            return 0
        now = time.time() if now is None else now
        removed = 0
        with self._directory_fd() as fd:
            for name, _ in self._entries(fd):
                try:
                    run, _ = self._read_at(fd, name, "run:" + name[:-5])
                except RunStoreError:
                    continue
                if run.expired(now):
                    os.unlink(name, dir_fd=fd)
                    removed += 1
            if removed:
                os.fsync(fd)
        return removed


def new_run_expiry(created_at: float, ttl_seconds: int = RUN_TTL_SECONDS) -> float:
    return created_at + ttl_seconds
