"""Run Store 测试（AR-04 §2-§3）：往返、权限、TTL、最早写入淘汰、finding 归属。"""
from __future__ import annotations

import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from openclean.core.errors import (
    FindingNotInRunError,
    RunExpiredError,
    RunNotFoundError,
    RunStoreError,
)
from openclean.core.identifiers import new_finding_id, new_run_id
from openclean.core.models import RUN_TTL_SECONDS, Run
from openclean.models import FileIdentity, Item
from openclean.runtime import run_store as run_store_module
from openclean.runtime.finding_projection import finding_from_item
from openclean.runtime.run_store import RunStore


def _run(now: float, *, expires_at: float | None = None) -> Run:
    return Run(
        run_id=new_run_id(),
        created_at=now,
        expires_at=now + RUN_TTL_SECONDS if expires_at is None else expires_at,
        requested_target="codex",
        openclean_version="0.23.0",
        macos_version="15.0",
        strategy_pack_hashes={"codex": "sha256:abc"},
        protect_config_hash="sha256:def",
        complete=True,
        finding_ids=(),
    )


def _finding(run_id: str) -> object:
    item = Item(
        path=Path("/Users/u/.codex/x"),
        size=4096,
        category="codex",
        safety="confirm",
        identity=FileIdentity(1, 2, 501),
        actionable=True,
        domain="ai",
    )
    return finding_from_item(
        item,
        finding_id=new_finding_id(),
        run_id=run_id,
        strategy_id="codex.marketplace.old-staging",
        strategy_version=1,
    )


class RunStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = RunStore(directory=Path(self._tmp.name) / "runs")

    def test_save_load_round_trip(self) -> None:
        now = time.time()
        run = _run(now)
        finding = _finding(run.run_id)
        run = Run(
            run_id=run.run_id,
            created_at=run.created_at,
            expires_at=run.expires_at,
            requested_target=run.requested_target,
            openclean_version=run.openclean_version,
            macos_version=run.macos_version,
            strategy_pack_hashes=dict(run.strategy_pack_hashes),
            protect_config_hash=run.protect_config_hash,
            complete=run.complete,
            issues=run.issues,
            finding_ids=(finding.finding_id,),
            strategy_versions={finding.strategy_id: finding.strategy_version},
        )
        self.store.save(run, [finding])
        self.assertEqual(self.store.load_run(run.run_id, now=now + 1), run)
        loaded = self.store.load_findings(run.run_id, now=now + 1)
        self.assertEqual(list(loaded), [finding])
        self.assertEqual(
            self.store.load_finding(run.run_id, finding.finding_id, now=now + 1),
            finding,
        )

    def test_private_permissions(self) -> None:
        run = _run(time.time())
        self.store.save(run, [])
        self.assertEqual(
            stat.S_IMODE(self.store.directory.stat().st_mode), 0o700
        )
        run_file = next(self.store.directory.iterdir())
        self.assertEqual(stat.S_IMODE(run_file.stat().st_mode), 0o600)

    def test_wide_file_permission_is_rejected(self) -> None:
        run = _run(time.time())
        self.store.save(run, [])
        run_file = next(self.store.directory.iterdir())
        os.chmod(run_file, 0o644)
        with self.assertRaises(RunStoreError):
            self.store.load_run(run.run_id)

    def test_wide_dir_permission_is_rejected(self) -> None:
        run = _run(time.time())
        self.store.save(run, [])
        os.chmod(self.store.directory, 0o755)
        with self.assertRaises(RunStoreError):
            self.store.load_run(run.run_id)

    def test_expired_run_rejected_and_removed(self) -> None:
        now = time.time()
        run = _run(now, expires_at=now + 10)
        self.store.save(run, [])
        with self.assertRaises(RunExpiredError):
            self.store.load_run(run.run_id, now=now + 11)
        # 过期后不自动重扫：再次读取是 not_found（文件已删）。
        with self.assertRaises(RunNotFoundError):
            self.store.load_run(run.run_id, now=now + 11)

    def test_missing_run(self) -> None:
        with self.assertRaises(RunNotFoundError):
            self.store.load_run(new_run_id())

    def test_finding_not_in_run(self) -> None:
        run = _run(time.time())
        self.store.save(run, [])
        with self.assertRaises(FindingNotInRunError):
            self.store.load_finding(run.run_id, new_finding_id())

    def test_oldest_written_capacity(self) -> None:
        with mock.patch.object(run_store_module, "MAX_RUNS", 3):
            now = time.time()
            for index in range(6):
                self.store.save(_run(now + index), [])
            remaining = list(self.store.directory.glob("*.json"))
            self.assertEqual(len(remaining), 3)

    def test_illegal_run_id_rejected(self) -> None:
        with self.assertRaises(RunStoreError):
            self.store.load_run("../escape")


if __name__ == "__main__":
    unittest.main()
