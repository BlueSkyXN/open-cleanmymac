"""Finding 驱动清理执行测试（AR-06 §1.1 核心验收 + §3 正负例）。

真实机器可能有 Codex 在跑，故 mock 掉 inspect 与 cleanup 两侧的进程/句柄快照捕获，
保证确定性；写操作全部在 TemporaryDirectory 内，绝不碰真实 HOME。
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

from openclean.actions.planner import execute_plan, resolve_plan
from openclean.cli import main
from openclean.core.models import (
    Action,
    Conditions,
    Detector,
    Locator,
    Provenance,
    Strategy,
    StrategyAssessment,
    StrategyPack,
)
from openclean.knowledge_base import KnowledgeBase
from openclean.predicates import ProtectionGate
from openclean.processes import OpenFileSnapshot, ProcessSnapshot
from openclean.runtime.inspect_service import inspect_target
from openclean.runtime.run_store import RunStore
from openclean.strategies.registry import StrategyRegistry

_EMPTY_PROC = ProcessSnapshot(())
_EMPTY_OPEN = OpenFileSnapshot(())


def _rules(home: Path) -> Path:
    rules = home / "rules.json"
    rules.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    return rules


def _trusted_pack() -> StrategyPack:
    """构建一个合成 trusted pack，供测试执行路径使用。"""
    strategy = Strategy(
        id="codex.test.old-staging",
        version=1,
        pack="codex",
        status="trusted",
        provenance=(Provenance(kind="personal_experience", observation_id="obs:test"),),
        locator=Locator(roots=("~/.codex/.tmp/marketplaces/.staging",)),
        detector=Detector(
            name="codex_transient",
            params={"subkind": "codex_marketplace_staging_targets", "name_glob": "marketplace-upgrade-*"},
        ),
        conditions=Conditions(minimum_age_days=7),
        action=Action(name="move_to_trash", supported=True),
        assessment=StrategyAssessment(
            classification="cleanup_candidate", certainty="high", action_risk="confirm"
        ),
    )
    return StrategyPack(name="codex", strategies=(strategy,))


class CleanExecTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.home = root / "home"
        self.home.mkdir()
        self.state = root / "state"
        self.runs = self.state / "openclean" / "runs"
        env = mock.patch.dict(
            os.environ, {"HOME": str(self.home), "XDG_STATE_HOME": str(self.state)}
        )
        env.start()
        self.addCleanup(env.stop)

        staging = self.home / ".codex/.tmp/marketplaces/.staging"
        self.old = staging / "marketplace-upgrade-old"
        self.old.mkdir(parents=True)
        (self.old / "blob.bin").write_bytes(b"x" * 8192)
        when = time.time() - 8 * 86400
        os.utime(self.old / "blob.bin", (when, when))
        os.utime(self.old, (when, when))

        self.protection = ProtectionGate(KnowledgeBase.empty())
        self.registry = StrategyRegistry.load(names=("codex",))
        self.store = RunStore(directory=self.runs)

    def _inspect(self):
        return inspect_target(
            "codex",
            self.protection,
            registry=self.registry,
            run_store=self.store,
            home=self.home,
            snapshots=(_EMPTY_PROC, _EMPTY_OPEN),
        )

    def _inspect_trusted(self):
        """Inspect with a synthetic trusted pack instead of codex (now report_only)."""
        registry = StrategyRegistry((_trusted_pack(),))
        return inspect_target(
            "codex",
            self.protection,
            registry=registry,
            run_store=self.store,
            home=self.home,
            snapshots=(_EMPTY_PROC, _EMPTY_OPEN),
        )

    def _old_finding(self, result):
        return next(
            f
            for f in result.findings
            if f.strategy_id == "codex.test.old-staging"
            and f.assessment.actionable
        )

    def test_execute_plan_moves_trusted_target_to_trash(self) -> None:
        result = self._inspect_trusted()
        finding = self._old_finding(result)
        registry = StrategyRegistry((_trusted_pack(),))
        plan = resolve_plan(
            run=result.run,
            findings=result.findings,
            selected_ids=[finding.finding_id],
            registry=registry,
            user_confirmed=True,
            include_confirm=True,
        )
        self.assertTrue(plan.plan_items[0].can_execute)
        with mock.patch(
            "openclean.cleanup.capture_process_snapshot", return_value=_EMPTY_PROC
        ):
            report, records = execute_plan(
                plan, list(result.findings), self.protection, home=self.home
            )
        self.assertTrue(report.complete)
        self.assertGreater(report.moved_bytes, 0)
        self.assertEqual(report.deleted_bytes, 0)
        self.assertEqual(records[0].status, "moved_to_trash")
        self.assertFalse(self.old.exists())
        self.assertTrue((self.home / ".Trash").exists())

    def _call(self, argv):
        buffer = io.StringIO()
        with mock.patch(
            "openclean.runtime.inspect_service.capture_process_snapshot",
            return_value=_EMPTY_PROC,
        ), mock.patch(
            "openclean.runtime.inspect_service.capture_open_file_snapshot",
            return_value=_EMPTY_OPEN,
        ), mock.patch(
            "openclean.cleanup.capture_process_snapshot", return_value=_EMPTY_PROC
        ), redirect_stdout(buffer):
            code = main(argv)
        return code, buffer.getvalue()

    def test_cli_preview_then_execute(self) -> None:
        trusted_registry = StrategyRegistry((_trusted_pack(),))
        rules = _rules(self.home)
        rs = ["--run-store", str(self.runs), "--rules", str(rules)]
        with mock.patch("openclean.cli._load_registry", return_value=trusted_registry):
            code, out = self._call(["inspect", "codex", "--json", *rs])
        self.assertEqual(code, 0)
        doc = json.loads(out)
        run_id = doc["run_id"]
        finding_id = next(
            f["finding_id"]
            for f in doc["findings"]
            if f["strategy_id"] == "codex.test.old-staging" and f["actionable"]
        )

        # 预览：不带 --yes 永不写
        with mock.patch("openclean.cli._load_registry", return_value=trusted_registry):
            code, out = self._call(
                ["clean", "--run", run_id, "--finding", finding_id, "--json", *rs]
            )
        self.assertEqual(code, 0)
        preview = json.loads(out)
        self.assertEqual(preview["mode"], "preview")
        self.assertFalse(preview["executed"])
        self.assertTrue(self.old.exists())
        self.assertFalse((self.home / ".Trash").exists())

        # 执行：--include-confirm --yes
        with mock.patch("openclean.cli._load_registry", return_value=trusted_registry):
            code, out = self._call(
                [
                    "clean",
                    "--run",
                    run_id,
                    "--finding",
                    finding_id,
                    "--include-confirm",
                    "--yes",
                    "--json",
                    *rs,
                ]
            )
        self.assertEqual(code, 0)
        executed = json.loads(out)
        self.assertTrue(executed["executed"])
        self.assertTrue(executed["outcome"]["complete"])
        self.assertGreater(executed["outcome"]["moved_to_trash_bytes"], 0)
        self.assertEqual(executed["outcome"]["permanently_deleted_bytes"], 0)
        self.assertEqual(
            executed["outcome"]["outcomes"][0]["status"], "moved_to_trash"
        )
        self.assertFalse(self.old.exists())

    def test_cli_yes_without_include_confirm_is_blocked(self) -> None:
        rules = _rules(self.home)
        rs = ["--run-store", str(self.runs), "--rules", str(rules)]
        _, out = self._call(["inspect", "codex", "--json", *rs])
        doc = json.loads(out)
        finding_id = next(
            f["finding_id"]
            for f in doc["findings"]
            if f["strategy_id"] == "codex.marketplace.old-staging" and f["actionable"]
        )
        code, out = self._call(
            [
                "clean",
                "--run",
                doc["run_id"],
                "--finding",
                finding_id,
                "--yes",
                "--json",
                *rs,
            ]
        )
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertFalse(payload["executed"])
        self.assertIn(
            "requires_include_confirm", payload["plan"]["items"][0]["block_reasons"]
        )
        self.assertTrue(self.old.exists())

    def test_cli_execute_active_summary_is_rejected(self) -> None:
        rules = _rules(self.home)
        rs = ["--run-store", str(self.runs), "--rules", str(rules)]
        _, out = self._call(["inspect", "codex", "--json", *rs])
        doc = json.loads(out)
        summary_id = next(
            f["finding_id"]
            for f in doc["findings"]
            if f["strategy_id"] == "codex.marketplace.staging-summary"
        )
        code, out = self._call(
            [
                "clean",
                "--run",
                doc["run_id"],
                "--finding",
                summary_id,
                "--include-critical",
                "--yes",
                "--json",
                *rs,
            ]
        )
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertFalse(payload["executed"])
        reasons = payload["plan"]["items"][0]["block_reasons"]
        self.assertIn("strategy_not_trusted", reasons)
        self.assertIn("aggregate_root_not_actionable", reasons)
        # 聚合根 .staging 必须仍在
        self.assertTrue(self.old.parent.exists())

    def test_cli_finding_not_in_run(self) -> None:
        rules = _rules(self.home)
        rs = ["--run-store", str(self.runs), "--rules", str(rules)]
        _, out = self._call(["inspect", "codex", "--json", *rs])
        run_id = json.loads(out)["run_id"]
        code, out = self._call(
            [
                "clean",
                "--run",
                run_id,
                "--finding",
                "finding:000000000000000000000000",
                "--yes",
                "--json",
                *rs,
            ]
        )
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["error"]["code"], "finding_not_in_run")


if __name__ == "__main__":
    unittest.main()
