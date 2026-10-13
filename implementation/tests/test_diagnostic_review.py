from __future__ import annotations

import contextlib
import copy
import curses
import io
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from openclean.cli import main
from openclean.diagnostic_review import EvidenceIndex
from openclean.models import Item, ScanResult
from openclean.tui import ReviewGroup, _run_review


class Screen:
    def __init__(self, keys, width=100):
        self.keys = list(keys)
        self.width = width
        self.lines = []

    def keypad(self, _enabled):
        pass

    def getmaxyx(self):
        return 24, self.width

    def erase(self):
        pass

    def addnstr(self, _row, _column, text, _length, _attribute=0):
        self.lines.append(text)

    def refresh(self):
        pass

    def getch(self):
        if not self.keys:
            raise AssertionError("测试按键已耗尽")
        return self.keys.pop(0)


class DiagnosticReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.enterContext(mock.patch.dict(os.environ, {"HOME": str(self.root)}))
        self.item = Item(self.root, 0, "下载文件审阅", "critical", domain="system",
                         resource_kind="filesystem_subset", diagnostic_kind="downloads",
                         actionable=False, preselected=False, resource_total_size=8192,
                         action_block_reason="仅审阅")
        self.records = [{
            "kind": "downloads", "category": self.item.category, "anchor_path": str(self.root),
            "complete": False,
            "files": [{"path": str(self.root / name), "file_type": "other",
                       "logical_bytes": 7, "allocated_bytes": 4096, "latest_mtime": 0,
                       "context": "user_files"} for name in ("first file.txt", "第二个文件.txt")],
        }]

    def test_entries_are_read_only_snapshot_data_and_do_not_mutate_source(self):
        before = copy.deepcopy(self.records)
        with mock.patch.object(Path, "stat", side_effect=AssertionError("禁止重新读文件")):
            entries = EvidenceIndex(self.records).entries(self.item)
        self.assertEqual(self.records, before)
        self.assertEqual(len(entries), 2)
        self.assertIn("所属报告完整：否", "\n".join(entries[0].lines))
        self.assertIn("1970-01-01T00:00:00+00:00", "\n".join(entries[0].lines))
        self.assertIn("mtime，不代表下载时间或最后使用时间", "\n".join(entries[0].lines))
        self.assertFalse(hasattr(entries[0], "actionable"))

    def test_same_anchor_kinds_do_not_borrow_each_others_evidence(self):
        image = {"kind": "disk_image", "category": "磁盘镜像候选", "anchor_path": str(self.root),
                 "complete": True, "files": [{"path": str(self.root / "image.img"),
                    "file_type": "disk_image", "context": "development_files", "mount_state": "unknown"}]}
        index = EvidenceIndex([*self.records, image])
        self.assertEqual(len(index.entries(self.item)), 2)
        entries = index.entries(replace(self.item, category=image["category"], diagnostic_kind="disk_image"))
        self.assertEqual(len(entries), 1)
        self.assertIn("挂载状态：未知", "\n".join(entries[0].lines))
        self.assertIn("文件上下文：开发文件", entries[0].lines)
        self.assertIn("已分配占用：未知", entries[0].lines)
        self.assertEqual(index.entries(replace(self.item, category="不同分类")), ())

    def test_language_and_binary_details_preserve_unknown_and_evidence_basis(self):
        for kind, category, detail in (
            ("application_languages", "应用语言包", {"localizations": [
                {"path": str(self.root / "fr.lproj"), "language": "fr", "resource_types": [".pak"],
                 "complete": True, "file_count": 1, "allocated_bytes": 4096, "logical_bytes": 8}]}),
            ("application_binaries", "通用二进制文件", {"files": [
                {"path": str(self.root / "lib.a"), "slices": [{"architecture": "x86_64",
                 "logical_bytes": 1024, "architecture_evidence": "fat_table_only"}],
                 "file_allocated_bytes": 4096, "other_architecture_logical_bytes": None}]}),
        ):
            item = replace(self.item, category=category,
                           diagnostic_kind="" if kind == "application_languages" else kind,
                           resource_kind="filesystem" if kind == "application_languages" else "filesystem_subset")
            record = {"kind": kind, "category": category, "anchor_path": str(self.root),
                      "complete": True, **detail}
            text = "\n".join(EvidenceIndex([record]).entries(item)[0].lines)
            self.assertIn("不提供选择或清理", text)
            if kind == "application_languages":
                self.assertIn("开发语言：未知", text)
                self.assertIn(".pak", text)
            else:
                self.assertIn("其它架构逻辑量：未知", text)
                self.assertIn("未验证归档内每个对象", text)

    def test_text_details_are_opt_in_and_use_same_evidence_for_scan_and_clean(self):
        for command in (["scan", "--domain", "system"], ["clean", "junk", "--no-interactive"]):
            outputs = []
            for options in ([], ["--details"]):
                result = ScanResult(items=[self.item], diagnostics=copy.deepcopy(self.records))
                stdout = io.StringIO()
                with mock.patch("openclean.cli.scan_domains", return_value=result), contextlib.redirect_stdout(stdout):
                    self.assertEqual(main([*command, *options]), 0)
                outputs.append(stdout.getvalue())
            self.assertNotIn("first file.txt", outputs[0])
            self.assertIn("first file.txt", outputs[1])
            self.assertIn("第二个文件.txt", outputs[1])
            self.assertIn("不改变选择", outputs[1])

    def test_json_details_remain_structured_and_redacted(self):
        result = ScanResult(items=[self.item], diagnostics=copy.deepcopy(self.records))
        stdout = io.StringIO()
        with mock.patch("openclean.cli.scan_domains", return_value=result), contextlib.redirect_stdout(stdout):
            status = main(["scan", "--domain", "system", "--details", "--json", "--redact-paths"])
        self.assertEqual(status, 0)
        payload = json.loads(stdout.getvalue())
        self.assertNotIn(str(self.root), stdout.getvalue())
        self.assertEqual(len(next(d for d in payload["diagnostics"] if d["kind"] == "downloads")["files"]), 2)

    def test_tui_file_navigation_preserves_existing_selection(self):
        safe = Item(self.root / "cache", 10, "缓存", "safe", domain="system", preselected=True)
        keys = [curses.KEY_RIGHT, curses.KEY_DOWN, ord("f"), ord(" "), ord("a"),
                10, ord(" "), curses.KEY_NPAGE, 27, curses.KEY_DOWN, 10, 27, 27,
                curses.KEY_LEFT, 10, ord("y")]
        screen = Screen(keys)
        with mock.patch("curses.curs_set"):
            result = _run_review(screen, (ReviewGroup("system", "系统", (safe, self.item)),),
                                 title="Review", allow_execution=True, diagnostics=tuple(self.records))
        self.assertEqual(result.selected, (safe,))
        self.assertTrue(result.execution_confirmed)
        output = "\n".join(screen.lines)
        self.assertIn("first file.txt", output)
        self.assertIn("第二个文件.txt", output)
        self.assertIn("不重新扫描", output)

    def test_tui_details_entry_and_cancel_do_not_select_resources(self):
        screen = Screen([curses.KEY_RIGHT, ord("i"), ord("f"), curses.KEY_END, 10, ord("q")], width=65)
        with mock.patch("curses.curs_set"):
            result = _run_review(screen, (ReviewGroup("system", "系统", (self.item,)),),
                                 title="Review", allow_execution=True, diagnostics=tuple(self.records))
        self.assertTrue(result.cancelled)
        self.assertEqual(result.selected, ())
        self.assertTrue(any("F 查看 2 项资源" in line for line in screen.lines))

    def test_terminal_controls_in_evidence_are_escaped(self):
        self.records[0]["files"][0]["path"] = str(self.root / "unsafe\x1b[2J.txt")
        result = ScanResult(items=[self.item], diagnostics=self.records)
        stdout = io.StringIO()
        with mock.patch("openclean.cli.scan_domains", return_value=result), contextlib.redirect_stdout(stdout):
            self.assertEqual(main(["scan", "--domain", "system", "--details"]), 0)
        self.assertNotIn("\x1b", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
