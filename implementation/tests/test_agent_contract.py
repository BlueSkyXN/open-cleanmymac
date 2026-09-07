"""Agent Contract 合同测试（AR-06 §3）。

聚焦跨切面、其他分阶段测试未显式覆盖的契约不变量：只返回目标 pack、ID 不含路径、
无任意路径删除入口、批量 identity 变化整批 fail-closed、KB protect 命中不进 detector、
脱敏输出不可 replay、命令面一致性。（#3/4/5/6/7/10/12/13 已在 test_agent_inspect /
test_agent_planner / test_agent_clean_exec / test_agent_run_store 覆盖。）
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from agent_fixtures import approved_registry

from openclean.actions.planner import execute_plan, resolve_plan
from openclean.cli import _command_from_argv, main
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


def _trusted_pack() -> StrategyPack:
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


class AgentContractTests(unittest.TestCase):
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
        self.registry = StrategyRegistry.load(names=("codex",))
        self.store = RunStore(directory=self.runs)

    def _staging(self) -> Path:
        return self.home / ".codex/.tmp/marketplaces/.staging"

    def _make_target(self, name: str, *, age_days: int = 8) -> Path:
        target = self._staging() / name
        target.mkdir(parents=True, exist_ok=True)
        blob = target / "blob.bin"
        blob.write_bytes(b"x" * 4096)
        when = time.time() - age_days * 86400
        os.utime(blob, (when, when))
        os.utime(target, (when, when))
        return target

    def _inspect(self, protection=None):
        return inspect_target(
            "codex",
            protection or ProtectionGate(KnowledgeBase.empty()),
            registry=self.registry,
            run_store=self.store,
            home=self.home,
            snapshots=(_EMPTY_PROC, _EMPTY_OPEN),
        )

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
        ), redirect_stdout(buffer), redirect_stderr(io.StringIO()):
            code = main(argv)
        return code, buffer.getvalue()

    # #1 只返回目标 pack
    def test_inspect_only_returns_target_pack(self) -> None:
        self._make_target("marketplace-upgrade-a")
        result = self._inspect()
        self.assertTrue(result.findings)
        for finding in result.findings:
            self.assertTrue(finding.strategy_id.startswith("codex."))

    # #2 finding_id/run_id 不含路径
    def test_ids_are_path_free(self) -> None:
        self._make_target("marketplace-upgrade-a")
        result = self._inspect()
        for finding in result.findings:
            for value in (finding.finding_id, finding.run_id):
                self.assertNotIn("/", value)
                self.assertNotIn(str(self.home), value)

    # #8 不存在任意路径删除入口
    def test_no_arbitrary_delete_entrypoint(self) -> None:
        for argv in (["delete", "/tmp/x"], ["rm", "/tmp/x"], ["remove", "--path", "/tmp/x"]):
            with self.subTest(argv=argv):
                code, _ = self._call(argv)
                self.assertEqual(code, 2)

    # #9 脱敏输出不可 replay
    def test_redacted_ids_not_replayable(self) -> None:
        self._make_target("marketplace-upgrade-a")
        rs = ["--run-store", str(self.runs)]
        code, out = self._call(["inspect", "codex", "--json", "--redact-paths", *rs])
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual(doc["run_id"], "run:redacted")
        self.assertFalse(doc["redaction"]["selection_replayable"])
        # 用脱敏后的 run_id 去 show 必须失败（不可 replay）
        code, out = self._call(
            ["show", "--run", "run:redacted", "--finding", "finding:redacted", "--json", *rs]
        )
        self.assertEqual(code, 1)

    # #11 批量执行中 identity 变化 → 整批 fail-closed
    def test_batch_identity_change_fails_closed(self) -> None:
        keep = self._make_target("marketplace-upgrade-keep")
        churn = self._make_target("marketplace-upgrade-churn")
        # P0: 使用合成 trusted pack 而非已降级的 codex
        registry = approved_registry((_trusted_pack(),))
        result = inspect_target(
            "codex",
            ProtectionGate(KnowledgeBase.empty()),
            registry=registry,
            run_store=self.store,
            home=self.home,
            snapshots=(_EMPTY_PROC, _EMPTY_OPEN),
        )
        targets = [
            f
            for f in result.findings
            if f.strategy_id == "codex.test.old-staging" and f.assessment.actionable
        ]
        self.assertEqual(len(targets), 2)
        plan = resolve_plan(
            run=result.run,
            findings=result.findings,
            selected_ids=[f.finding_id for f in targets],
            registry=registry,
            user_confirmed=True,
            include_confirm=True,
        )
        self.assertTrue(all(i.can_execute for i in plan.plan_items))
        # 执行前替换 churn 目标（inode 变化）
        churn.rename(churn.with_name("original-churn"))
        churn.mkdir()
        (churn / "other.bin").write_bytes(b"z" * 4096)
        with mock.patch(
            "openclean.cleanup.capture_process_snapshot", return_value=_EMPTY_PROC
        ):
            report, records = execute_plan(
                plan, list(result.findings), ProtectionGate(KnowledgeBase.empty()), home=self.home,
                run=result.run, registry=registry, user_confirmed=True,
                include_confirm=True, snapshots=(_EMPTY_PROC, _EMPTY_OPEN)
            )
        # all-or-nothing：identity 变化导致整批不执行，keep 目标仍在
        self.assertFalse(report.complete)
        self.assertEqual(report.moved_bytes, 0)
        self.assertTrue(keep.exists())
        self.assertTrue(all(r.status in {"blocked", "not_run"} for r in records))

    # #14 KB protect 命中 → 不进 detector
    def test_protected_path_not_detected(self) -> None:
        self._make_target("marketplace-upgrade-a")
        kb = KnowledgeBase.from_mapping(
            {"schema_version": 1, "protect": {"paths": [str(self._staging())]}}
        )
        result = self._inspect(ProtectionGate(kb))
        marketplace = [
            f
            for f in result.findings
            if f.strategy_id
            in {"codex.marketplace.old-staging", "codex.marketplace.staging-summary"}
        ]
        self.assertEqual(marketplace, [])

    # #15 命令面一致性
    def test_command_surface_consistency(self) -> None:
        self.assertEqual(_command_from_argv(["inspect", "codex"]), "inspect")
        self.assertEqual(_command_from_argv(["show", "--run", "r"]), "show")
        self.assertEqual(_command_from_argv(["strategy", "list"]), "strategy list")
        self.assertEqual(_command_from_argv(["clean", "--run", "r"]), "clean")
        # inspect all 覆盖已加载 pack
        self._make_target("marketplace-upgrade-a")
        result = inspect_target(
            "all",
            ProtectionGate(KnowledgeBase.empty()),
            registry=self.registry,
            run_store=self.store,
            home=self.home,
            snapshots=(_EMPTY_PROC, _EMPTY_OPEN),
        )
        self.assertEqual(result.run.requested_target, "all")
        self.assertTrue(
            all(f.strategy_id.startswith("codex.") for f in result.findings)
        )


if __name__ == "__main__":
    unittest.main()
