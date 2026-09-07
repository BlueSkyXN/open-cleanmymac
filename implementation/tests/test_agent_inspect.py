"""inspect 编排 + CLI inspect/show 测试（AR-03 §2/§4 / AR-06 §2）。

真实机器可能有 Codex 在跑，故统一注入/ mock 空的进程与句柄快照，保证确定性。
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from openclean.core.errors import PackNotFoundError
from openclean.knowledge_base import KnowledgeBase
from openclean.predicates import ProtectionGate
from openclean.processes import OpenFileSnapshot, ProcessSnapshot
from openclean.runtime.inspect_service import inspect_target
from openclean.runtime.run_store import RunStore
from openclean.strategies.registry import StrategyRegistry

_EMPTY_SNAPSHOTS = (ProcessSnapshot(()), OpenFileSnapshot(()))


class _Env(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.home = root / "home"
        self.home.mkdir()
        self.runs = root / "state" / "openclean" / "runs"
        self._env = mock.patch.dict(
            os.environ,
            {"HOME": str(self.home), "XDG_STATE_HOME": str(root / "state")},
        )
        self._env.start()
        self.addCleanup(self._env.stop)
        self.protection = ProtectionGate(KnowledgeBase.empty())
        self.registry = StrategyRegistry.load(names=("codex",))
        self.store = RunStore(directory=self.runs)

    def _make_staging(self, name: str, *, age_days: int) -> Path:
        staging = self.home / ".codex/.tmp/marketplaces/.staging"
        target = staging / name
        target.mkdir(parents=True, exist_ok=True)
        blob = target / "blob.bin"
        blob.write_bytes(b"x" * 4096)
        when = time.time() - age_days * 86400
        os.utime(blob, (when, when))
        os.utime(target, (when, when))
        return target


class InspectServiceTests(_Env):
    def test_codex_per_target_and_summary(self) -> None:
        old = self._make_staging("marketplace-upgrade-old", age_days=8)
        self._make_staging("marketplace-upgrade-new", age_days=1)
        result = inspect_target(
            "codex",
            self.protection,
            registry=self.registry,
            run_store=self.store,
            home=self.home,
            snapshots=_EMPTY_SNAPSHOTS,
        )
        self.assertEqual(result.run.requested_target, "codex")
        self.assertTrue(result.run.complete)
        by_strategy: dict[str, list] = {}
        for finding in result.findings:
            by_strategy.setdefault(finding.strategy_id, []).append(finding)

        # 逐目标 trusted 策略：old 可执行，new 因年龄不足被阻断
        targets = by_strategy["codex.marketplace.old-staging"]
        self.assertEqual(len(targets), 2)
        old_f = next(f for f in targets if f.target.display_path == str(old))
        self.assertTrue(old_f.assessment.actionable)
        self.assertEqual(old_f.assessment.action_risk, "confirm")
        self.assertEqual(old_f.target.kind, "filesystem")
        self.assertIsNotNone(old_f.target.identity)
        new_f = next(f for f in targets if f is not old_f)
        self.assertFalse(new_f.assessment.actionable)
        self.assertTrue(new_f.assessment.block_reasons)

        # 聚合 summary：filesystem_subset，永不 actionable（AR-06 §4）
        summary = by_strategy["codex.marketplace.staging-summary"][0]
        self.assertEqual(summary.target.kind, "filesystem_subset")
        self.assertFalse(summary.assessment.actionable)

    def test_run_persisted_and_ids_are_path_free(self) -> None:
        self._make_staging("marketplace-upgrade-old", age_days=8)
        result = inspect_target(
            "codex",
            self.protection,
            registry=self.registry,
            run_store=self.store,
            home=self.home,
            snapshots=_EMPTY_SNAPSHOTS,
        )
        loaded = self.store.load_run(result.run.run_id)
        self.assertEqual(loaded.run_id, result.run.run_id)
        self.assertEqual(loaded.requested_target, "codex")
        self.assertIn("codex", loaded.strategy_pack_hashes)
        for finding in result.findings:
            self.assertNotIn("/", finding.finding_id)
            self.assertEqual(finding.run_id, result.run.run_id)
        self.assertEqual(
            set(result.run.finding_ids),
            {f.finding_id for f in result.findings},
        )

    def test_running_codex_blocks_action(self) -> None:
        self._make_staging("marketplace-upgrade-old", age_days=8)
        running = (ProcessSnapshot(("/App/Codex.app/Contents/MacOS/codex",)), OpenFileSnapshot(()))
        result = inspect_target(
            "codex",
            self.protection,
            registry=self.registry,
            run_store=self.store,
            home=self.home,
            snapshots=running,
        )
        targets = [
            f
            for f in result.findings
            if f.strategy_id == "codex.marketplace.old-staging"
        ]
        self.assertTrue(targets)
        self.assertTrue(all(not f.assessment.actionable for f in targets))

    def test_all_target_loads_codex(self) -> None:
        result = inspect_target(
            "all",
            self.protection,
            registry=self.registry,
            run_store=self.store,
            home=self.home,
            snapshots=_EMPTY_SNAPSHOTS,
        )
        self.assertEqual(result.run.requested_target, "all")

    def test_unknown_pack_raises(self) -> None:
        with self.assertRaises(PackNotFoundError):
            inspect_target(
                "qoder",
                self.protection,
                registry=self.registry,
                run_store=self.store,
                home=self.home,
                snapshots=_EMPTY_SNAPSHOTS,
            )


class InspectShowCliTests(_Env):
    def _call(self, argv: list[str]) -> tuple[int, str]:
        from openclean.cli import main

        buffer = io.StringIO()
        with mock.patch(
            "openclean.runtime.inspect_service.capture_process_snapshot",
            return_value=ProcessSnapshot(()),
        ), mock.patch(
            "openclean.runtime.inspect_service.capture_open_file_snapshot",
            return_value=OpenFileSnapshot(()),
        ), redirect_stdout(buffer):
            code = main(argv)
        return code, buffer.getvalue()

    def test_inspect_then_show(self) -> None:
        self._make_staging("marketplace-upgrade-old", age_days=8)
        code, out = self._call(
            ["inspect", "codex", "--json", "--run-store", str(self.runs)]
        )
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual(doc["requested_target"], "codex")
        self.assertTrue(doc["run_id"].startswith("run:"))
        self.assertGreaterEqual(doc["totals"]["findings"], 1)
        finding_id = doc["findings"][0]["finding_id"]

        code, out = self._call(
            [
                "show",
                "--run",
                doc["run_id"],
                "--finding",
                finding_id,
                "--json",
                "--run-store",
                str(self.runs),
            ]
        )
        self.assertEqual(code, 0)
        shown = json.loads(out)
        self.assertEqual(shown["finding_id"], finding_id)
        self.assertIn("evidence", shown)
        self.assertIn("target", shown)

    def test_show_finding_not_in_run(self) -> None:
        self._make_staging("marketplace-upgrade-old", age_days=8)
        _, out = self._call(
            ["inspect", "codex", "--json", "--run-store", str(self.runs)]
        )
        run_id = json.loads(out)["run_id"]
        code, out = self._call(
            [
                "show",
                "--run",
                run_id,
                "--finding",
                "finding:000000000000000000000000",
                "--json",
                "--run-store",
                str(self.runs),
            ]
        )
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["error"]["code"], "finding_not_in_run")

    def test_inspect_unavailable_target_exit_1(self) -> None:
        code, out = self._call(
            ["inspect", "qoder", "--json", "--run-store", str(self.runs)]
        )
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["error"]["code"], "pack_not_found")

    def test_inspect_unknown_target_exit_2(self) -> None:
        code, _ = self._call(["inspect", "bogus", "--json"])
        self.assertEqual(code, 2)

    def test_redacted_ids_not_replayable(self) -> None:
        self._make_staging("marketplace-upgrade-old", age_days=8)
        code, out = self._call(
            [
                "inspect",
                "codex",
                "--json",
                "--redact-paths",
                "--run-store",
                str(self.runs),
            ]
        )
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual(doc["run_id"], "run:redacted")
        self.assertFalse(doc["redaction"]["selection_replayable"])
        for finding in doc["findings"]:
            self.assertEqual(finding["finding_id"], "finding:redacted")


if __name__ == "__main__":
    unittest.main()
