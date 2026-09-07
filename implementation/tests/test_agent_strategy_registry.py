"""Strategy 加载/校验/hash/注册表测试（AR-02 / AR-07 阶段 A）。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from openclean.core.errors import PackNotFoundError, StrategyError
from openclean.strategies.registry import (
    StrategyRegistry,
    available_pack_names,
    load_pack,
    pack_hash,
    validate_pack,
)

_VALID_PACK = {
    "schema_version": 1,
    "name": "codex",
    "strategies": [
        {
            "id": "codex.marketplace.old-staging",
            "version": 1,
            "pack": "codex",
            "status": "trusted",
            "provenance": [
                {"kind": "personal_experience", "observation_id": "obs:personal:codex:1"}
            ],
            "locator": {"roots": ["~/.codex/.tmp/marketplaces/.staging"]},
            "detector": {
                "name": "codex_transient",
                "params": {"subkind": "codex_marketplace_staging"},
            },
            "conditions": {
                "minimum_age_days": 7,
                "require_structure_match": True,
                "require_no_open_handles": True,
            },
            "guards": {"do_not_generalize_parent": True, "protect_running_processes": True},
            "assessment": {
                "classification": "cleanup_candidate",
                "certainty": "high",
                "action_risk": "confirm",
            },
            "recommendation": {
                "summary": "旧 Marketplace 升级暂存",
                "do_not_do": ["不能把 .staging 父目录当垃圾"],
            },
            "action": {"name": "move_to_trash", "supported": True},
        },
        {
            "id": "codex.logs.sqlite-freelist",
            "version": 1,
            "pack": "codex",
            "status": "active",
            "provenance": [{"kind": "ai_research", "observation_id": "obs:ai:codex:2"}],
            "detector": {"name": "sqlite_freelist"},
            "action": {"name": "report_only", "supported": False},
        },
        {
            "id": "codex.draft.hypothesis",
            "version": 1,
            "pack": "codex",
            "status": "draft",
            "detector": {"name": "retention"},
            "action": {"name": "report_only", "supported": False},
        },
    ],
}


def _write_pack(directory: Path, name: str, payload: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.packs_dir = Path(self._tmp.name)
        _write_pack(self.packs_dir, "codex", _VALID_PACK)

    def test_load_and_enum(self) -> None:
        self.assertEqual(available_pack_names(packs_dir=self.packs_dir), ("codex",))
        pack = load_pack("codex", packs_dir=self.packs_dir)
        self.assertEqual(pack.name, "codex")
        self.assertEqual(len(pack.strategies), 3)
        self.assertEqual(pack.strategies[0].detector.params["subkind"], "codex_marketplace_staging")

    def test_runtime_visible_excludes_draft(self) -> None:
        registry = StrategyRegistry.load(packs_dir=self.packs_dir)
        visible = {s.id for s in registry.runtime_visible()}
        self.assertEqual(
            visible,
            {"codex.marketplace.old-staging", "codex.logs.sqlite-freelist"},
        )
        self.assertEqual(registry.loaded_pack_names(), ("codex",))

    def test_get_by_id(self) -> None:
        registry = StrategyRegistry.load(packs_dir=self.packs_dir)
        self.assertEqual(
            registry.get("codex.logs.sqlite-freelist").detector.name, "sqlite_freelist"
        )
        with self.assertRaises(StrategyError):
            registry.get("codex.does.not.exist")

    def test_pack_hash_stable_and_sensitive(self) -> None:
        pack = load_pack("codex", packs_dir=self.packs_dir)
        self.assertTrue(pack_hash(pack).startswith("sha256:"))
        self.assertEqual(pack_hash(pack), pack_hash(load_pack("codex", packs_dir=self.packs_dir)))
        # 改内容（提升某策略 version）后 hash 必变，供执行前 strategy_hash_matches 比对。
        mutated = json.loads(json.dumps(_VALID_PACK))
        mutated["strategies"][1]["version"] = 2
        _write_pack(self.packs_dir, "codex", mutated)
        self.assertNotEqual(pack_hash(pack), pack_hash(load_pack("codex", packs_dir=self.packs_dir)))

    def test_missing_pack_raises(self) -> None:
        with self.assertRaises(PackNotFoundError):
            load_pack("nope", packs_dir=self.packs_dir)

    def test_unknown_detector_rejected(self) -> None:
        bad = json.loads(json.dumps(_VALID_PACK))
        bad["strategies"][1]["detector"]["name"] = "rm_rf"
        _write_pack(self.packs_dir, "codex", bad)
        with self.assertRaisesRegex(StrategyError, "detector"):
            load_pack("codex", packs_dir=self.packs_dir)

    def test_unknown_action_rejected(self) -> None:
        bad = json.loads(json.dumps(_VALID_PACK))
        bad["strategies"][0]["action"] = {"name": "shell_out", "supported": True}
        _write_pack(self.packs_dir, "codex", bad)
        with self.assertRaisesRegex(StrategyError, "action"):
            load_pack("codex", packs_dir=self.packs_dir)

    def test_unknown_field_rejected(self) -> None:
        bad = json.loads(json.dumps(_VALID_PACK))
        bad["strategies"][0]["executable"] = "rm -rf /"
        _write_pack(self.packs_dir, "codex", bad)
        with self.assertRaisesRegex(StrategyError, "未知字段"):
            load_pack("codex", packs_dir=self.packs_dir)

    def test_bad_schema_version_rejected(self) -> None:
        bad = json.loads(json.dumps(_VALID_PACK))
        bad["schema_version"] = 99
        _write_pack(self.packs_dir, "codex", bad)
        with self.assertRaisesRegex(StrategyError, "schema_version"):
            load_pack("codex", packs_dir=self.packs_dir)

    def test_validate_pack_accepts_valid(self) -> None:
        validate_pack(load_pack("codex", packs_dir=self.packs_dir))


if __name__ == "__main__":
    unittest.main()
