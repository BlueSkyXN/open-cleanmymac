from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from openclean.analyzer import AnalyzeError, analyze_path
from openclean.cli import main
from openclean.engine import IgnoreRules
from openclean.knowledge_base import KnowledgeBase


class AnalyzerTests(unittest.TestCase):
    def test_rejects_dataless_root_without_enumerating_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root_stat = root.lstat()
            original_lstat = Path.lstat

            def fake_lstat(path: Path):
                stat_result = original_lstat(path)
                if path != root:
                    return stat_result
                return SimpleNamespace(
                    st_mode=root_stat.st_mode,
                    st_size=root_stat.st_size,
                    st_blocks=root_stat.st_blocks,
                    st_mtime=root_stat.st_mtime,
                    st_dev=root_stat.st_dev,
                    st_ino=root_stat.st_ino,
                    st_nlink=root_stat.st_nlink,
                    st_uid=root_stat.st_uid,
                    st_flags=0x40000000,
                )

            with mock.patch(
                "openclean.models.MACOS_SF_DATALESS", 0x40000000
            ), mock.patch.object(Path, "lstat", new=fake_lstat), mock.patch(
                "openclean.analyzer.os.scandir"
            ) as scandir, self.assertRaisesRegex(AnalyzeError, "dataless"):
                analyze_path(root)

            scandir.assert_not_called()

    def test_analyzes_first_level_sorted_and_applies_protection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            large = root / "large"
            protected = root / "protected"
            large.mkdir()
            protected.mkdir()
            large_file = large / "data.bin"
            large_file.write_bytes(b"x" * 10_000)
            (protected / "secret.bin").write_bytes(b"x" * 20)
            small_file = root / "file.bin"
            small_file.write_bytes(b"yy")
            knowledge_base = KnowledgeBase.from_mapping(
                {
                    "schema_version": 1,
                    "protect": {"paths": [str(protected)]},
                }
            )

            analysis = analyze_path(
                root,
                protection=IgnoreRules(knowledge_base=knowledge_base),
            )

            self.assertTrue(analysis.complete)
            self.assertEqual(
                [entry.item.path for entry in analysis.entries],
                [large, root / "file.bin"],
            )
            large_allocated = large_file.stat().st_blocks * 512
            small_allocated = small_file.stat().st_blocks * 512
            expected_total = large_allocated + small_allocated
            self.assertEqual(analysis.total, expected_total)
            self.assertAlmostEqual(
                analysis.entries[0].percent,
                large_allocated / expected_total * 100,
            )
            self.assertIsNotNone(analysis.volume_total)
            self.assertIsNotNone(analysis.volume_free)

    def test_cli_analyze_json_top_limits_entries_not_total(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "data"
            root.mkdir()
            large = root / "large.bin"
            small = root / "small.bin"
            large.write_bytes(b"x" * 10_000)
            small.write_bytes(b"x" * 2)
            rules = base / "rules.json"
            rules.write_text(
                json.dumps({"schema_version": 1}), encoding="utf-8"
            )
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                status = main(
                    [
                        "analyze",
                        str(root),
                        "--rules",
                        str(rules),
                        "--top",
                        "1",
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(status, 0)
            self.assertEqual(payload["command"], "analyze")
            self.assertEqual(payload["mode"], "browse")
            self.assertEqual(payload["root"], str(root))
            expected_total = (
                large.stat().st_blocks + small.stat().st_blocks
            ) * 512
            self.assertEqual(payload["total_bytes"], expected_total)
            self.assertEqual(payload["top"], 1)
            self.assertTrue(payload["truncated"])
            self.assertEqual(payload["entry_count_total"], 2)
            self.assertEqual(payload["entry_count_returned"], 1)
            self.assertEqual(len(payload["entries"]), 1)
            self.assertEqual(
                payload["entries"][0]["path"], str(root / "large.bin")
            )

    def test_rejects_file_and_symlink_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            file_path = root / "file.bin"
            file_path.write_bytes(b"content")
            link = root / "link"
            link.symlink_to(root, target_is_directory=True)

            with self.assertRaisesRegex(AnalyzeError, "不是目录"):
                analyze_path(file_path)
            with self.assertRaisesRegex(AnalyzeError, "符号链接"):
                analyze_path(link)

    def test_rejects_root_with_symlink_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            target = base / "target"
            child = target / "child"
            child.mkdir(parents=True)
            alias = base / "alias"
            alias.symlink_to(target, target_is_directory=True)

            with self.assertRaisesRegex(AnalyzeError, "符号链接组件"):
                analyze_path(alias / "child")

    def test_cli_invalid_path_returns_usage_error_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing"
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                status = main(["analyze", str(missing)])

            self.assertEqual(status, 2)
            self.assertFalse(missing.exists())
            self.assertIn("不存在", stderr.getvalue())

    def test_cli_text_top_marks_truncated_view(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "large.bin").write_bytes(b"x" * 10_000)
            (root / "small.bin").write_bytes(b"x")
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                status = main(
                    ["analyze", str(root), "--top", "1", "--no-interactive"]
                )

            self.assertEqual(status, 0)
            self.assertIn("仅显示最大的 1/2 项", stdout.getvalue())
            self.assertIn("合计仍包含全部项目", stdout.getvalue())

    def test_structural_system_boundary_marks_entry_non_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            protected = root / "protected"
            protected.mkdir()
            (protected / "data.bin").write_bytes(b"data")

            with mock.patch(
                "openclean.analyzer.nonprivileged_action_block_reason",
                return_value="系统保护路径：测试",
            ):
                analysis = analyze_path(root)

            item = analysis.entries[0].item
            self.assertFalse(item.actionable)
            self.assertEqual(item.safety, "critical")
            self.assertIn("系统保护路径", item.action_block_reason)


if __name__ == "__main__":
    unittest.main()
