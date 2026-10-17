from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openclean.cleanup import execute_cleanup
from openclean.cli import _item_payload
from openclean.codex_home import scan_codex_home
from openclean.diagnostic_review import EvidenceIndex
from openclean.engine import IgnoreRules, scan_domains
from openclean.redaction import JsonPathRedactor
from openclean.scanpoints import AI_TOOL_JUNK


class CodexHomeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.root = self.home / ".codex"
        self.root.mkdir()
        self.enterContext(mock.patch.dict(os.environ, {"HOME": str(self.home)}))

    def write(self, name, text="data"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def scan(self, protection=None):
        return scan_codex_home(protection or IgnoreRules(), root=self.root)

    def test_classifies_materials_without_reading_them_or_core_data(self):
        self.write("config.toml", 'model_catalog_json = "model-catalog.json"\n')
        self.write("review.config.toml", 'model_instructions_file = "prompt.md"\n')
        self.write("model-catalog.json", "SECRET_CATALOG_BODY")
        self.write("prompt.md", "SECRET_INSTRUCTIONS")
        self.write("notes.md", "SECRET_NOTES")
        self.write("config-example.toml", "SECRET_EXAMPLE")
        self.write("models_cache.json.bak-20000101", "SECRET_BACKUP")
        self.write(".DS_Store", "finder")
        self.write("auth.json", "SECRET_AUTH")
        self.write("state_99.sqlite-wal", "SECRET_DATABASE")
        self.write(".sandbox_migration", "v1")
        self.write("shell_snapshots/session.sh", "SECRET_SHELL")
        self.write("docs/nested.txt", "SECRET_NESTED")
        self.write("new-component.json", "SECRET_UNKNOWN")
        result = self.scan()
        record = result.diagnostics[0]
        rows = {Path(row["path"]).name: row for row in record["files"]}
        self.assertTrue(result.complete)
        self.assertEqual(rows["model-catalog.json"]["classification"], "configured_reference")
        self.assertEqual(rows["prompt.md"]["classification"], "configured_reference")
        self.assertEqual(rows["review.config.toml"]["classification"], "supported_profile")
        self.assertEqual(rows["notes.md"]["classification"], "supplemental_document")
        self.assertEqual(rows["config-example.toml"]["classification"], "backup_or_example")
        self.assertEqual(rows[".DS_Store"]["classification"], "finder_metadata")
        self.assertEqual(rows["new-component.json"]["classification"], "unclassified")
        self.assertIsNone(rows["docs"]["logical_bytes"])
        self.assertNotIn("nested.txt", rows)
        for name in ["config.toml", "auth.json", "state_99.sqlite-wal", ".sandbox_migration", "shell_snapshots"]:
            self.assertNotIn(name, rows)
        self.assertNotIn("SECRET_", json.dumps(record))
        item = result.items[0]
        self.assertFalse(item.actionable)
        self.assertFalse(item.preselected)
        self.assertEqual(_item_payload(item)["reclaimable_bytes"], 0)
        report = execute_cleanup(
            [item], IgnoreRules(), home=self.home,
            trash_resolver=lambda _path: self.fail("只读诊断不得访问 Trash"),
        )
        self.assertEqual(report.outcomes[0].status, "blocked")
        self.assertTrue((self.root / "notes.md").exists())

    def test_protection_symlinks_and_core_files_precede_config_content_reads(self):
        ignored = self.write("hidden.md")
        outside = self.home / "outside"
        outside.mkdir()
        (outside / "secret").write_text("not scanned")
        (self.root / "linked").symlink_to(outside, target_is_directory=True)
        (self.root / "config.toml").symlink_to(outside / "secret")
        self.write("visible.md")
        with mock.patch("openclean.codex_home.open_resource", side_effect=AssertionError("不应读正文")):
            result = self.scan(IgnoreRules([str(ignored)]))
        names = [Path(row["path"]).name for row in result.diagnostics[0]["files"]]
        self.assertEqual(names, ["visible.md"])
        alternate = self.home / "linked-home"
        alternate.symlink_to(self.root, target_is_directory=True)
        self.assertEqual(scan_codex_home(IgnoreRules(), root=alternate).items, [])

    def test_broken_or_oversized_config_does_not_claim_complete_reference_check(self):
        self.write("notes.md")
        config = self.write("config.toml", 'model_catalog_json = "unterminated')
        result = self.scan()
        self.assertFalse(result.complete)
        self.assertFalse(result.diagnostics[0]["reference_scan_complete"])
        self.assertNotIn("unterminated", str(result.issues))
        config.write_bytes(b"x" * (1024 * 1024 + 1))
        with mock.patch("openclean.codex_home.open_resource", side_effect=AssertionError("大文件不读取")):
            self.assertFalse(self.scan().complete)

    def test_limit_preserves_partial_evidence(self):
        for name in ["a.md", "b.md", "c.md"]:
            self.write(name)
        with mock.patch("openclean.codex_home._MAX_ENTRIES", 2):
            result = self.scan()
        self.assertFalse(result.complete)
        self.assertEqual(len(result.diagnostics[0]["files"]), 2)
        self.assertFalse(result.diagnostics[0]["complete"])

    def test_deeply_nested_config_preserves_materials_with_incomplete_references(self):
        self.write("notes.md")
        self.write("config.toml", 'private_value = ' + '[' * 1500 + '"SECRET"' + ']' * 1500)
        result = self.scan()
        self.assertFalse(result.complete)
        self.assertFalse(result.diagnostics[0]["reference_scan_complete"])
        self.assertEqual([Path(row["path"]).name for row in result.diagnostics[0]["files"]], ["notes.md"])
        self.assertEqual(result.issues[0].code, "config_reference_unavailable")
        self.assertNotIn("SECRET", str(result.issues))

    def test_hardlink_sizes_are_not_double_counted(self):
        first = self.write("first.md", "content")
        os.link(first, self.root / "second.md")
        result = self.scan()
        self.assertEqual(result.items[0].logical_size, len("content"))
        self.assertEqual(len(result.diagnostics[0]["files"]), 2)

    def test_details_and_redaction_use_same_snapshot(self):
        self.write("notes.md")
        result = self.scan()
        with mock.patch.object(Path, "stat", side_effect=AssertionError("详情不重新读文件")):
            entries = EvidenceIndex(result.diagnostics).entries(result.items[0])
        self.assertIn("额外文本材料", entries[0].summary)
        self.assertIn("未检查项目、脚本或内嵌模板", "\n".join(entries[0].lines))
        redacted = JsonPathRedactor().redact({"diagnostics": result.diagnostics})
        self.assertNotIn(str(self.home), json.dumps(redacted))

    def test_ai_scan_dispatches_registered_home_scanner(self):
        self.write("notes.md")
        point = next(p for p in AI_TOOL_JUNK if p.scanner == "codex-home")
        with mock.patch("openclean.engine.DOMAINS", {"ai": [point]}):
            result = scan_domains(["ai"], workers=1)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].domain, "ai")
        self.assertEqual(result.diagnostics[0]["kind"], "codex_home")
