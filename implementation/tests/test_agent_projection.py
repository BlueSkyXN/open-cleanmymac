"""Item ↔ Finding 往返等价与安全字段保全测试（AR-01 §6 / 计划 A4 硬门）。

这是阶段 A 的验收门：``item_from_finding(finding_from_item(x)) == x`` 必须对既有测试里
出现过的每一种 Item 形态成立，且 ``_audit_item`` 的硬阻断字段（excluded_paths /
cloud_file_count / is_cloud_file / requires_privilege / identity / domain / updater 三字段）
在投影后原样保留（否则 R1：静默放宽安全闸）。
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from openclean.core.models import ID_PREFIX_FINDING, ID_PREFIX_RUN
from openclean.models import FileIdentity, Item
from openclean.runtime.finding_projection import (
    finding_from_item,
    item_from_finding,
    item_from_payload,
    item_to_payload,
)

_KW = {
    "finding_id": f"{ID_PREFIX_FINDING}abc",
    "run_id": f"{ID_PREFIX_RUN}xyz",
    "strategy_id": "codex.marketplace.old-staging",
    "strategy_version": 1,
}


def _corpus() -> list[Item]:
    """覆盖各资源类型/诊断类型/安全字段的合法 Item 语料。"""
    return [
        # 1 最小可执行文件系统项
        Item(path=Path("/Users/u/a"), size=10, category="c"),
        # 2 Docker 资源（非文件系统，带 binding）
        Item(
            path=None,
            size=5,
            category="docker",
            resource_kind="docker",
            identifier="docker:build-cache",
            resource_binding="ctx|host|engine",
            domain="developer",
            requires_explicit_selection=True,
        ),
        # 3 retention 只读诊断（含进程 marker 与句柄）
        Item(
            path=Path("/Users/u/logs"),
            size=0,
            category="logs",
            diagnostic_kind="retention",
            actionable=False,
            retention_file_count=3,
            retention_7d_bytes=300,
            retention_14d_bytes=200,
            retention_30d_bytes=100,
            open_handle_count=1,
            running_process_markers=("codex", "ChatGPT.app"),
            domain="ai",
        ),
        # 4 filesystem_subset codex_transient 聚合（部分测量）
        Item(
            path=Path("/Users/u/.staging"),
            size=100,
            category="codex",
            resource_kind="filesystem_subset",
            diagnostic_kind="codex_transient",
            actionable=False,
            total_count=5,
            measured_count=3,
            measurement_complete=False,
            open_handle_count=0,
            identity=FileIdentity(1, 2, 501),
        ),
        # 5 sqlite_freelist 只读
        Item(
            path=Path("/Users/u/db.sqlite"),
            size=1000,
            category="db",
            diagnostic_kind="sqlite_freelist",
            actionable=False,
            sqlite_page_size=4096,
            sqlite_page_count=100,
            sqlite_freelist_count=10,
            sqlite_internal_free_bytes=40960,
            sqlite_internal_free_ratio=0.1,
            sqlite_wal_bytes=0,
        ),
        # 6 crashpad_pairing 只读
        Item(
            path=Path("/Users/u/crashpad"),
            size=50,
            category="crash",
            resource_kind="filesystem_subset",
            diagnostic_kind="crashpad_pairing",
            actionable=False,
            total_count=5,
            paired_artifact_count=2,
            recent_artifact_count=1,
        ),
        # 7 updater_temp 只读
        Item(
            path=Path("/Users/u/ShipIt"),
            size=200,
            category="updater",
            diagnostic_kind="updater_temp",
            actionable=False,
            updater_status="pending_update",
            installed_version="1.0",
            staged_version="2.0",
        ),
        # 8 open_unlinked 只读（potential_bytes=0，逻辑上限）
        Item(
            path=Path("/Users/u"),
            size=0,
            category="deleted-open",
            resource_kind="filesystem_subset",
            diagnostic_kind="open_unlinked",
            actionable=False,
            related_process_count=2,
            total_count=3,
            open_handle_count=4,
            logical_size=999,
        ),
        # 9 darwin-user-cache 特殊清理范围（critical + 精确选择）
        Item(
            path=Path("/Users/u/Library/Caches/sub"),
            size=10,
            category="cache",
            cleanup_scope="darwin-user-cache",
            cleanup_root=Path("/Users/u/Library/Caches"),
            cleanup_root_identity=FileIdentity(1, 2, 501),
            identity=FileIdentity(1, 3, 501),
            actionable=True,
            safety="critical",
            requires_explicit_selection=True,
            domain="system",
        ),
        # 10 云占位 + excluded + 特权 + 跨卷（全部硬阻断字段）
        Item(
            path=Path("/Users/u/cloud"),
            size=0,
            category="c",
            is_cloud_file=True,
            cloud_file_count=2,
            cloud_logical_size=500,
            excluded_paths=1,
            cross_device_paths=1,
            actionable=False,
            action_block_reason="cloud-placeholder",
            requires_privilege=True,
            identity=FileIdentity(1, 9, 501),
            domain="system",
            age_days=30,
            latest_mtime=123.0,
            allocated_size=0,
            logical_size=0,
            preselected=False,
            note="n",
            project_root=Path("/Users/u/proj"),
            artifact_name="node_modules",
        ),
        # 11 失效启动项（startup_program + PATH 引用）
        Item(
            path=Path("/Users/u/LaunchAgents/broken.plist"),
            size=1,
            category="startup",
            startup_program="/nonexistent",
            startup_program_uses_path=True,
            actionable=True,
            safety="critical",
            requires_explicit_selection=True,
            identity=FileIdentity(1, 5, 501),
            domain="system",
        ),
    ]


class RoundTripTests(unittest.TestCase):
    def test_item_survives_finding_round_trip(self) -> None:
        for index, item in enumerate(_corpus()):
            with self.subTest(index=index, category=item.category):
                finding = finding_from_item(item, **_KW)
                self.assertEqual(item_from_finding(finding), item)

    def test_payload_survives_json_round_trip(self) -> None:
        # Run Store 会经 json.dumps/loads；快照必须仍是 JSON 安全且可精确重建。
        for index, item in enumerate(_corpus()):
            with self.subTest(index=index):
                payload = json.loads(json.dumps(item_to_payload(item)))
                self.assertEqual(item_from_payload(payload), item)

    def test_finding_is_json_serializable(self) -> None:
        for item in _corpus():
            finding = finding_from_item(item, **_KW)
            # evidence.payload 必须可直接序列化（Run Store / show 输出依赖此）。
            json.dumps(finding.evidence.payload)


class SafetyFieldPreservationTests(unittest.TestCase):
    """R1 缓解：硬阻断字段必须在投影后原样保留。"""

    def test_hard_block_fields_preserved(self) -> None:
        item = _corpus()[9]  # 云占位 + excluded + 特权 + 跨卷
        rebuilt = item_from_finding(finding_from_item(item, **_KW))
        self.assertEqual(rebuilt.excluded_paths, 1)
        self.assertEqual(rebuilt.cloud_file_count, 2)
        self.assertTrue(rebuilt.is_cloud_file)
        self.assertTrue(rebuilt.requires_privilege)
        self.assertEqual(rebuilt.cross_device_paths, 1)
        self.assertEqual(rebuilt.identity, FileIdentity(1, 9, 501))
        self.assertEqual(rebuilt.domain, "system")

    def test_updater_fields_preserved(self) -> None:
        item = _corpus()[6]
        rebuilt = item_from_finding(finding_from_item(item, **_KW))
        self.assertEqual(rebuilt.updater_status, "pending_update")
        self.assertEqual(rebuilt.installed_version, "1.0")
        self.assertEqual(rebuilt.staged_version, "2.0")

    def test_readonly_diagnostic_projects_not_actionable(self) -> None:
        for item in _corpus():
            if item.diagnostic_kind:
                with self.subTest(kind=item.diagnostic_kind):
                    finding = finding_from_item(item, **_KW)
                    self.assertFalse(finding.assessment.actionable)
                    self.assertEqual(finding.evidence.kind, item.diagnostic_kind)

    def test_action_risk_uses_safety_levels(self) -> None:
        item = _corpus()[8]  # safety="critical"
        finding = finding_from_item(item, **_KW)
        self.assertEqual(finding.assessment.action_risk, "critical")


if __name__ == "__main__":
    unittest.main()
