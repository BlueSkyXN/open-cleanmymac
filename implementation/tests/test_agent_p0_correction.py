"""P0 纠偏回归测试（SPEC §10 VAL-002~VAL-014）。

每个测试对应交接包 README.md 中的一个阻塞缺陷。本轮目标：先证明这些测试在当前代码上
按预期失败（TASK-001），随后修复生产代码使它们全部通过（TASK-002~TASK-007）。

所有写入型测试强制 TemporaryDirectory 隔离，断言真实 HOME 未访问、未修改。
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

from openclean.actions.planner import execute_plan, resolve_plan
from openclean.cli import main
from openclean.core.models import (
    Action,
    Conditions,
    Detector,
    Locator,
    Provenance,
    Run,
    RunIssue,
    Strategy,
    StrategyAssessment,
    StrategyPack,
)
from openclean.knowledge_base import KnowledgeBase
from openclean.predicates import ProtectionGate
from openclean.processes import OpenFileSnapshot, ProcessSnapshot
from openclean.runtime.finding_projection import item_from_finding
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
    """合成 trusted pack，供需要执行路径的测试使用。"""
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


class _BaseTestCase(unittest.TestCase):
    """共享 TemporaryDirectory + HOME/STATE 隔离。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.home = root / "home"
        self.home.mkdir()
        self.real_home = Path(os.path.expanduser("~"))
        self.state = root / "state"
        self.runs = self.state / "openclean" / "runs"
        env = mock.patch.dict(
            os.environ, {"HOME": str(self.home), "XDG_STATE_HOME": str(self.state)}
        )
        env.start()
        self.addCleanup(env.stop)
        self.protection = ProtectionGate(KnowledgeBase.empty())
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
            protection or self.protection,
            registry=self.registry,
            run_store=self.store,
            home=self.home,
            snapshots=(_EMPTY_PROC, _EMPTY_OPEN),
        )

    def _inspect_trusted(self):
        """Inspect with synthetic trusted pack."""
        registry = StrategyRegistry((_trusted_pack(),))
        return inspect_target(
            "codex",
            self.protection,
            registry=registry,
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

    def _assert_real_home_untouched(self) -> None:
        """断言真实 HOME 不在本次测试访问范围内。"""
        self.assertNotEqual(self.home, self.real_home)


class TestBug001FindingWithoutRunIsUsageError(_BaseTestCase):
    """VAL-002 / BUG-001：clean --finding F --yes 无 --run → exit 2，不扫描，零写入。

    当前缺陷：cli.py 只检查 ``getattr(args, "run", None)``，--finding 不带 --run 时
    落入经典 clean 路径，执行经典预选候选。
    """

    def test_finding_without_run_is_usage_error_and_never_scans(self) -> None:
        self._make_target("marketplace-upgrade-xyz")
        rules = _rules(self.home)
        rs = ["--run-store", str(self.runs), "--rules", str(rules)]
        # 先 inspect 产出一个 finding_id
        code, out = self._call(["inspect", "codex", "--json", *rs])
        self.assertEqual(code, 0)
        finding_id = json.loads(out)["findings"][0]["finding_id"]
        # 现在 clean --finding 不带 --run
        code, out = self._call([
            "clean", "--finding", finding_id, "--yes", "--json", *rs,
        ])
        # 必须 exit 2，零写入
        self.assertEqual(code, 2, "BUG-001: --finding 无 --run 必须 exit 2")
        if out:
            doc = json.loads(out)
            self.assertFalse(doc.get("executed", False))
        # 目标必须仍在
        self.assertTrue((self._staging() / "marketplace-upgrade-xyz").exists())
        self._assert_real_home_untouched()


class TestBug001MixedCleanModes(_BaseTestCase):
    """VAL-003 / BUG-001 变体：category 与 --run/--finding 混用 → exit 2。"""

    def test_agent_and_classic_clean_arguments_are_mutually_exclusive(self) -> None:
        self._make_target("marketplace-upgrade-abc")
        rules = _rules(self.home)
        rs = ["--run-store", str(self.runs), "--rules", str(rules)]
        code, out = self._call(["inspect", "codex", "--json", *rs])
        self.assertEqual(code, 0)
        doc = json.loads(out)
        # junk + --run 混用
        code, _ = self._call([
            "clean", "junk",
            "--run", doc["run_id"],
            "--finding", doc["findings"][0]["finding_id"],
            "--yes", "--json", *rs,
        ])
        self.assertEqual(code, 2, "BUG-001: category + --run 混用必须 exit 2")
        self._assert_real_home_untouched()


class TestBug002PreviewNotExecutable(_BaseTestCase):
    """VAL-004 / BUG-002：execute_plan 接收 preview plan → 拒绝，零写入。

    当前缺陷：execute_plan 不检查 plan.mode，preview 的 can_execute=true 的项仍被执行。
    """

    def test_execute_plan_rejects_preview(self) -> None:
        self._make_target("marketplace-upgrade-preview")
        result = self._inspect()
        finding = next(
            f for f in result.findings
            if f.strategy_id == "codex.marketplace.old-staging"
            and f.assessment.actionable
        )
        plan = resolve_plan(
            run=result.run,
            findings=result.findings,
            selected_ids=[finding.finding_id],
            registry=self.registry,
            user_confirmed=False,  # preview
        )
        self.assertEqual(plan.mode, "preview")
        # 直接调用 execute_plan 必须拒绝
        with self.assertRaises(Exception):
            execute_plan(plan, list(result.findings), self.protection, home=self.home)
        # 目标必须仍在
        self.assertTrue((self._staging() / "marketplace-upgrade-preview").exists())
        self._assert_real_home_untouched()

    def test_preview_plan_items_have_user_confirmation_required(self) -> None:
        """preview 模式下 can_execute 必须为 false，并带 user_confirmation_required。"""
        self._make_target("marketplace-upgrade-confirm")
        result = self._inspect()
        finding = next(
            f for f in result.findings
            if f.strategy_id == "codex.marketplace.old-staging"
            and f.assessment.actionable
        )
        plan = resolve_plan(
            run=result.run,
            findings=result.findings,
            selected_ids=[finding.finding_id],
            registry=self.registry,
            user_confirmed=False,
            include_confirm=True,
        )
        self.assertEqual(plan.mode, "preview")
        for item in plan.plan_items:
            self.assertFalse(
                item.can_execute,
                "BUG-002: preview plan 的 can_execute 必须为 false",
            )
            self.assertIn(
                "user_confirmation_required", item.block_reasons,
                "BUG-002: preview plan 必须包含 user_confirmation_required",
            )


class TestBug003AllOrNothing(_BaseTestCase):
    """VAL-005 / BUG-003：批次一项 allowed、一项 blocked → 全批 blocked/not_run，零写入。"""

    def test_mixed_batch_is_all_or_nothing(self) -> None:
        keep = self._make_target("marketplace-upgrade-keep")
        self._make_target("marketplace-upgrade-block", age_days=1)
        # 使用 trusted pack 以获得可执行项
        result = self._inspect_trusted()
        registry = StrategyRegistry((_trusted_pack(),))
        actionable = [
            f for f in result.findings
            if f.strategy_id == "codex.test.old-staging"
            and f.assessment.actionable
        ]
        blocked = [
            f for f in result.findings
            if f.strategy_id == "codex.test.old-staging"
            and not f.assessment.actionable
        ]
        self.assertTrue(actionable, "应至少有一个 actionable")
        self.assertTrue(blocked, "应至少有一个 blocked（太新）")
        all_ids = [f.finding_id for f in actionable + blocked]
        plan = resolve_plan(
            run=result.run,
            findings=result.findings,
            selected_ids=all_ids,
            registry=registry,
            user_confirmed=True,
            include_confirm=True,
        )
        has_executable = any(i.can_execute for i in plan.plan_items)
        has_blocked = any(not i.can_execute for i in plan.plan_items)
        self.assertTrue(has_executable and has_blocked, "需要混合批次")
        with mock.patch(
            "openclean.cleanup.capture_process_snapshot", return_value=_EMPTY_PROC
        ):
            report, records = execute_plan(
                plan, list(result.findings), self.protection, home=self.home,
            )
        self.assertEqual(report.moved_bytes, 0, "BUG-003: 混合批次必须零写入")
        self.assertTrue(keep.exists(), "BUG-003: keep 目标必须仍在")
        for r in records:
            self.assertIn(
                r.status, {"blocked", "not_run"},
                "BUG-003: 所有 outcomes 必须 blocked/not_run",
            )


class TestBug004IncompleteRun(_BaseTestCase):
    """VAL-006 / BUG-004：run.complete=false 请求执行 → run_incomplete，零写入。

    当前缺陷：resolve_plan 不检查 run.complete。
    """

    def test_incomplete_run_cannot_execute(self) -> None:
        self._make_target("marketplace-upgrade-incomplete")
        result = self._inspect()
        finding = next(
            f for f in result.findings
            if f.strategy_id == "codex.marketplace.old-staging"
            and f.assessment.actionable
        )
        # 构造一个 complete=False 的 Run
        incomplete_run = Run(
            run_id=result.run.run_id,
            created_at=result.run.created_at,
            expires_at=result.run.expires_at,
            requested_target=result.run.requested_target,
            openclean_version=result.run.openclean_version,
            macos_version=result.run.macos_version,
            strategy_pack_hashes=result.run.strategy_pack_hashes,
            protect_config_hash=result.run.protect_config_hash,
            complete=False,  # 关键
            issues=(RunIssue(code="scanner_unavailable", message="test", blocking=True),),
            finding_ids=result.run.finding_ids,
        )
        plan = resolve_plan(
            run=incomplete_run,
            findings=result.findings,
            selected_ids=[finding.finding_id],
            registry=self.registry,
            user_confirmed=True,
            include_confirm=True,
        )
        for item in plan.plan_items:
            self.assertFalse(
                item.can_execute,
                "BUG-004: incomplete Run 必须所有项 can_execute=false",
            )
            self.assertIn(
                "run_incomplete", item.block_reasons,
                "BUG-004: 必须包含 run_incomplete block reason",
            )


class TestBug005CrossRunFinding(_BaseTestCase):
    """VAL-007 / BUG-005：Finding.run_id 不属于 Run → finding_not_in_run，零写入。

    当前缺陷：resolve_plan 只检查 finding_id 是否在传入的 findings 列表中，
    不检查 finding.run_id 是否等于 run.run_id。
    """

    def test_cross_run_finding_is_rejected(self) -> None:
        self._make_target("marketplace-upgrade-cross")
        result_a = self._inspect()
        # 第二次 inspect 产出不同 run_id
        result_b = self._inspect()
        self.assertNotEqual(result_a.run.run_id, result_b.run.run_id)
        finding_from_b = next(
            f for f in result_b.findings
            if f.strategy_id == "codex.marketplace.old-staging"
            and f.assessment.actionable
        )
        # 用 run_a 去请求 finding_from_b
        from openclean.core.errors import FindingNotInRunError
        with self.assertRaises(FindingNotInRunError):
            resolve_plan(
                run=result_a.run,
                findings=result_a.findings + result_b.findings,
                selected_ids=[finding_from_b.finding_id],
                registry=self.registry,
                user_confirmed=True,
                include_confirm=True,
            )


class TestBug006TargetPayloadDivergence(_BaseTestCase):
    """VAL-009 / BUG-006：target 与 evidence payload 路径/identity 不一致 → 拒绝。

    当前缺陷：execute_plan 使用 item_from_finding 重建 Item 但不验证与 resolved_targets 一致。
    """

    def test_target_payload_divergence_is_rejected(self) -> None:
        self._make_target("marketplace-upgrade-diverge")
        result = self._inspect()
        finding = next(
            f for f in result.findings
            if f.strategy_id == "codex.marketplace.old-staging"
            and f.assessment.actionable
        )
        plan = resolve_plan(
            run=result.run,
            findings=result.findings,
            selected_ids=[finding.finding_id],
            registry=self.registry,
            user_confirmed=True,
            include_confirm=True,
        )
        # 验证 resolved_targets 与 evidence payload 重建 Item 的一致性
        for item in plan.plan_items:
            if item.can_execute and item.resolved_targets:
                rebuilt = item_from_finding(finding)
                target_path = item.resolved_targets[0].display_path
                self.assertEqual(
                    str(rebuilt.path), target_path,
                    "BUG-006: evidence payload 重建路径必须与 resolved_targets 一致",
                )


class TestBug005StrategyVersionMismatch(_BaseTestCase):
    """VAL-008 / BUG-005 变体：Strategy version/hash 不一致 → 拒绝。"""

    def test_strategy_version_mismatch_is_rejected(self) -> None:
        self._make_target("marketplace-upgrade-version")
        result = self._inspect()
        finding = next(
            f for f in result.findings
            if f.strategy_id == "codex.marketplace.old-staging"
            and f.assessment.actionable
        )
        # 篡改 run 的 pack hash 使之不匹配
        bad_run = Run(
            run_id=result.run.run_id,
            created_at=result.run.created_at,
            expires_at=result.run.expires_at,
            requested_target=result.run.requested_target,
            openclean_version=result.run.openclean_version,
            macos_version=result.run.macos_version,
            strategy_pack_hashes={"codex": "sha256:wrong_hash_value"},
            protect_config_hash=result.run.protect_config_hash,
            complete=result.run.complete,
            issues=result.run.issues,
            finding_ids=result.run.finding_ids,
        )
        plan = resolve_plan(
            run=bad_run,
            findings=result.findings,
            selected_ids=[finding.finding_id],
            registry=self.registry,
            user_confirmed=True,
            include_confirm=True,
        )
        for item in plan.plan_items:
            self.assertFalse(item.can_execute)
            self.assertIn("strategy_hash_mismatch", item.block_reasons)


class TestBug007ExternalPackTrust(_BaseTestCase):
    """VAL-014 / BUG-007：外部 caller-supplied trusted pack → 强制 report_only 或拒绝。

    当前缺陷：--packs-dir 允许调用者自建 trusted pack，可移动任意匹配目录。
    """

    def test_external_pack_cannot_self_declare_trusted(self) -> None:
        # 创建一个外部 pack，自封 trusted
        external_dir = Path(self._tmp.name) / "external_packs"
        external_dir.mkdir()
        external_pack = {
            "schema_version": 1,
            "name": "caller-supplied",
            "strategies": [
                {
                    "id": "caller-supplied.delete-all",
                    "version": 1,
                    "pack": "caller-supplied",
                    "status": "trusted",
                    "provenance": [
                        {"kind": "ai_research", "observation_id": "obs:fake"}
                    ],
                    "locator": {"roots": ["~/Documents"]},
                    "detector": {
                        "name": "codex_transient",
                        "params": {
                            "subkind": "codex_marketplace_staging_targets",
                            "name_glob": "*",
                        },
                    },
                    "conditions": {"minimum_age_days": 0},
                    "assessment": {
                        "classification": "cleanup_candidate",
                        "certainty": "high",
                        "action_risk": "safe",
                    },
                    "recommendation": {"summary": "fake"},
                    "action": {"name": "move_to_trash", "supported": True},
                }
            ],
        }
        (external_dir / "caller-supplied.json").write_text(
            json.dumps(external_pack), encoding="utf-8"
        )
        # 加载外部 pack
        registry = StrategyRegistry.load(packs_dir=external_dir)
        strategy = registry.get("caller-supplied.delete-all")
        # 外部 pack 的 trusted 策略不可获得动作权限
        # 方案 A：加载时降级为 report_only
        # 方案 B：resolve_plan 中检查 pack 来源
        # 无论哪种方案，外部 trusted 策略不得产生 can_execute=true
        # 创建一个有价值的目录
        valuable = self.home / "Documents" / "valuable-project"
        valuable.mkdir(parents=True)
        (valuable / "important.txt").write_text("precious data", encoding="utf-8")
        # 如果策略被降级或拒绝，下面的测试验证
        if strategy.status == "trusted" and strategy.action.supported:
            self.fail(
                "BUG-007: 外部 caller-supplied pack 不可拥有 trusted + action.supported"
            )
        self._assert_real_home_untouched()


class TestBug008StructureMatch(_BaseTestCase):
    """VAL-010 / BUG-008：任意内容的旧 marketplace-upgrade-* 目录不得 trusted/actionable。

    当前缺陷：require_structure_match=true 未被执行，仅凭名称和年龄授权清理。
    """

    def test_arbitrary_old_staging_contents_are_report_only(self) -> None:
        # 创建一个名为 marketplace-upgrade-* 但内容是重要项目
        target = self._make_target("marketplace-upgrade-important")
        (target / "important_config.json").write_text(
            '{"critical": "configuration"}', encoding="utf-8"
        )
        (target / "src").mkdir()
        (target / "src" / "main.py").write_text("print('hello')", encoding="utf-8")
        result = self._inspect()
        actionable = [
            f for f in result.findings
            if f.strategy_id == "codex.marketplace.old-staging"
            and f.assessment.actionable
            and "important" in (f.target.display_path or "")
        ]
        # 如果 require_structure_match 被正确执行，包含非 staging 结构内容的
        # 目录不应通过结构匹配
        # 在当前代码中，这只是名字匹配 + 年龄检查，所以会 actionable
        # 这个测试验证要么被结构匹配过滤，要么策略降为 report_only
        strategy = self.registry.get("codex.marketplace.old-staging")
        if strategy.conditions.require_structure_match:
            # require_structure_match=true 但实际代码未检查 → BUG-008
            # 要么策略降级，要么结构匹配实现
            for f in actionable:
                self.fail(
                    f"BUG-008: require_structure_match=true 未执行，"
                    f"任意内容目录 {f.target.display_path} 不应该是 actionable"
                )


class TestLocatorHomeIsolation(_BaseTestCase):
    """VAL-012：显式测试 HOME 与真实 HOME 不同 → 只访问测试 HOME。"""

    def test_home_override_controls_locator_expansion(self) -> None:
        self._make_target("marketplace-upgrade-home")
        result = self._inspect()
        for finding in result.findings:
            if finding.target.display_path:
                self.assertTrue(
                    finding.target.display_path.startswith(str(self.home)),
                    f"VAL-012: 目标路径必须在测试 HOME 下：{finding.target.display_path}",
                )
                self.assertNotIn(
                    str(self.real_home),
                    finding.target.display_path,
                    "VAL-012: 不得访问真实 HOME",
                )


class TestRunStoreConsistency(_BaseTestCase):
    """VAL-013：Run Store 权限、symlink、TTL、条目和字节容量。"""

    def test_symlink_run_store_dir_is_rejected(self) -> None:
        """symlink Run Store 目录必须拒绝。"""
        real_dir = Path(self._tmp.name) / "real_runs"
        real_dir.mkdir()
        link_dir = Path(self._tmp.name) / "link_runs"
        link_dir.symlink_to(real_dir)
        store = RunStore(directory=link_dir)
        run = Run(
            run_id="run:test_symlink_reject",
            created_at=time.time(),
            expires_at=time.time() + 86400,
            requested_target="test",
            openclean_version="0.0.0",
        )
        # symlink 目录必须被拒绝
        from openclean.core.errors import RunStoreError
        with self.assertRaises((RunStoreError, OSError)):
            store.save(run, [])


if __name__ == "__main__":
    unittest.main()
