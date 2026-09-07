"""本地私有 Run Store（AR-04 §2-§3）。

存储一次 ``inspect`` 的 Run manifest 与其 Finding 集合，供后续 ``show``/``clean`` 跨命令按
``run_id``/``finding_id`` 解析。安全约束：

- 位置：``$XDG_STATE_HOME/openclean/runs``，回退 ``~/.local/state/openclean/runs``；
- 权限：目录 ``0700``、文件 ``0600``；读回时校验，过宽 → ``RunStoreError`` fail-closed；
- TTL：默认 24h（写时算 ``expires_at``）；过期 → ``RunExpiredError``，**绝不自动重扫**；
- 写入：原子写，逐行克隆 ``config.py`` ``ConfigStore._write``（mkstemp+fchmod 0600+fsync+
  os.replace+chmod 0600+finally 清理）；
- 容量：条目上限 + LRU 淘汰；
- 标识：``run_id``/``finding_id`` 不编码路径（见 core.identifiers）。
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
import time
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..core.errors import (
    FindingNotInRunError,
    RunExpiredError,
    RunNotFoundError,
    RunStoreError,
)
from ..core.models import (
    ID_PREFIX_RUN,
    RUN_TTL_SECONDS,
    Finding,
    FindingAssessment,
    FindingEvidence,
    FindingMeasurement,
    FindingTarget,
    Recommendation,
    Run,
    RunIssue,
)
from ..models import FileIdentity, normalize_path

MAX_RUNS = 64


def default_run_store_dir() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(os.path.expanduser(xdg)) if xdg else Path("~/.local/state").expanduser()
    return normalize_path(base / "openclean" / "runs")


def finding_to_dict(finding: Finding) -> dict[str, Any]:
    return asdict(finding)


def finding_from_dict(data: dict[str, Any]) -> Finding:
    target = data["target"]
    identity = target.get("identity")
    assessment = data["assessment"]
    evidence = data["evidence"]
    recommendation = data.get("recommendation", {})
    measurement = data["measurement"]
    return Finding(
        finding_id=data["finding_id"],
        run_id=data["run_id"],
        strategy_id=data["strategy_id"],
        strategy_version=data["strategy_version"],
        target=FindingTarget(
            kind=target["kind"],
            display_path=target.get("display_path"),
            identifier=target.get("identifier", ""),
            identity=(
                FileIdentity(
                    device=identity["device"],
                    inode=identity["inode"],
                    owner=identity["owner"],
                )
                if identity
                else None
            ),
        ),
        measurement=FindingMeasurement(
            size=measurement.get("size", 0),
            allocated_bytes=measurement.get("allocated_bytes"),
            logical_bytes=measurement.get("logical_bytes"),
            age_days=measurement.get("age_days"),
            latest_mtime=measurement.get("latest_mtime"),
        ),
        assessment=FindingAssessment(
            classification=assessment["classification"],
            certainty=assessment["certainty"],
            action_risk=assessment["action_risk"],
            actionable=assessment["actionable"],
            block_reasons=tuple(assessment.get("block_reasons", ())),
            requires_privilege=assessment.get("requires_privilege", False),
            is_cloud_file=assessment.get("is_cloud_file", False),
            requires_explicit_selection=assessment.get(
                "requires_explicit_selection", False
            ),
        ),
        evidence=FindingEvidence(
            kind=evidence["kind"], payload=dict(evidence.get("payload", {}))
        ),
        recommendation=Recommendation(
            summary=recommendation.get("summary", ""),
            do_not_do=tuple(recommendation.get("do_not_do", ())),
        ),
    )


def run_to_dict(run: Run) -> dict[str, Any]:
    return asdict(run)


def run_from_dict(data: dict[str, Any]) -> Run:
    return Run(
        run_id=data["run_id"],
        created_at=data["created_at"],
        expires_at=data["expires_at"],
        requested_target=data["requested_target"],
        openclean_version=data["openclean_version"],
        macos_version=data.get("macos_version", ""),
        strategy_pack_hashes=dict(data.get("strategy_pack_hashes", {})),
        protect_config_hash=data.get("protect_config_hash", ""),
        complete=data.get("complete", True),
        issues=tuple(
            RunIssue(
                code=issue["code"],
                message=issue["message"],
                blocking=issue.get("blocking", True),
            )
            for issue in data.get("issues", ())
        ),
        finding_ids=tuple(data.get("finding_ids", ())),
    )


class RunStore:
    """Run + Finding 的本机私有存储。"""

    def __init__(self, directory: str | os.PathLike[str] | None = None) -> None:
        self.directory = normalize_path(directory or default_run_store_dir())

    # --- 路径与权限 ---

    def _run_path(self, run_id: str) -> Path:
        if not run_id.startswith(ID_PREFIX_RUN):
            raise RunStoreError(f"非法 run_id：{run_id}")
        token = run_id[len(ID_PREFIX_RUN):]
        if not token or "/" in token or token in {".", ".."}:
            raise RunStoreError(f"非法 run_id token：{run_id}")
        return self.directory / f"{token}.json"

    def _assert_private_dir(self) -> None:
        try:
            mode = stat.S_IMODE(self.directory.stat().st_mode)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise RunStoreError(f"无法检查 Run Store 目录：{exc}") from exc
        if mode & 0o077:
            raise RunStoreError(
                f"Run Store 目录权限过宽（{oct(mode)}），拒绝读写；应为 0700"
            )

    def _assert_private_file(self, path: Path) -> None:
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError as exc:
            raise RunStoreError(f"无法检查 Run 文件：{exc}") from exc
        if mode & 0o077:
            raise RunStoreError(
                f"Run 文件权限过宽（{oct(mode)}），拒绝读取；应为 0600"
            )

    # --- 写入 ---

    def save(self, run: Run, findings: Sequence[Finding]) -> None:
        self._assert_private_dir()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        # 目录已存在时 mkdir 不改权限，显式收紧一次。
        os.chmod(self.directory, 0o700)
        payload = {
            "run": run_to_dict(run),
            "findings": [finding_to_dict(f) for f in findings],
        }
        self._write_atomic(self._run_path(run.run_id), payload)
        self._enforce_capacity()

    def _write_atomic(self, path: Path, payload: dict[str, Any]) -> None:
        parent = path.parent
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=parent
        )
        temporary = Path(temporary_name)
        descriptor_open = True
        try:
            os.fchmod(descriptor, 0o600)
            stream = os.fdopen(descriptor, "w", encoding="utf-8")
            descriptor_open = False
            with stream:
                json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        except OSError as exc:
            raise RunStoreError(f"无法写入 Run：{exc}") from exc
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

    def _enforce_capacity(self) -> None:
        try:
            entries = [
                p for p in self.directory.iterdir() if p.suffix == ".json"
            ]
        except OSError:
            return
        if len(entries) <= MAX_RUNS:
            return
        entries.sort(key=lambda p: p.stat().st_mtime)
        for stale in entries[: len(entries) - MAX_RUNS]:
            try:
                stale.unlink()
            except OSError:
                continue

    # --- 读取 ---

    def _read_payload(self, run_id: str, *, now: float) -> dict[str, Any]:
        self._assert_private_dir()
        path = self._run_path(run_id)
        if not path.exists():
            raise RunNotFoundError(f"Run 不存在：{run_id}")
        self._assert_private_file(path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RunStoreError(f"无法读取 Run {run_id}：{exc}") from exc
        run = run_from_dict(payload["run"])
        if run.expired(now):
            # 过期即拒绝；删除以避免残留，绝不自动重扫复用同一 run_id。
            try:
                path.unlink()
            except OSError:
                pass
            raise RunExpiredError(f"Run 已过期：{run_id}")
        return payload

    def load_run(self, run_id: str, *, now: float | None = None) -> Run:
        now = time.time() if now is None else now
        payload = self._read_payload(run_id, now=now)
        return run_from_dict(payload["run"])

    def load_findings(
        self, run_id: str, *, now: float | None = None
    ) -> tuple[Finding, ...]:
        now = time.time() if now is None else now
        payload = self._read_payload(run_id, now=now)
        return tuple(finding_from_dict(f) for f in payload.get("findings", []))

    def load_finding(
        self, run_id: str, finding_id: str, *, now: float | None = None
    ) -> Finding:
        for finding in self.load_findings(run_id, now=now):
            if finding.finding_id == finding_id:
                return finding
        raise FindingNotInRunError(
            f"Finding {finding_id} 不属于 Run {run_id}"
        )

    def prune_expired(self, *, now: float | None = None) -> int:
        now = time.time() if now is None else now
        removed = 0
        try:
            entries = [p for p in self.directory.iterdir() if p.suffix == ".json"]
        except OSError:
            return 0
        for path in entries:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                run = run_from_dict(payload["run"])
            except (OSError, json.JSONDecodeError, KeyError, ValueError):
                continue
            if run.expired(now):
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    continue
        return removed


def new_run_expiry(created_at: float, ttl_seconds: int = RUN_TTL_SECONDS) -> float:
    return created_at + ttl_seconds
