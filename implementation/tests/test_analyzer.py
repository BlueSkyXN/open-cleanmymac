from __future__ import annotations

import contextlib
import errno
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from openclean.analyzer import AnalyzeError, analyze_path
from openclean.cli import main
from openclean.engine import IgnoreRules
from openclean.knowledge_base import KnowledgeBase
from openclean.models import FileFacts


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
            self.assertEqual(payload["reclaimable_bytes"], 0)
            self.assertEqual(payload["entries"][0]["reclaimable_bytes"], 0)
            self.assertTrue(payload["volumes"])
            self.assertTrue(
                all(
                    volume["reclaimable_bytes"] == 0
                    for volume in payload["volumes"]
                )
            )
            self.assertEqual(payload["entries"][0]["safety"], "critical")
            self.assertTrue(
                payload["entries"][0]["requires_explicit_selection"]
            )
            self.assertEqual(
                payload["entries"][0]["path"], str(root / "large.bin")
            )

    def test_analyze_stays_on_each_top_level_items_device(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            parent = root / "mounts"
            external = parent / "external"
            external.mkdir(parents=True)
            local_file = parent / "local.bin"
            local_file.write_bytes(b"local")
            (external / "large.bin").write_bytes(b"x" * 100_000)
            engine = __import__(
                "openclean.engine", fromlist=["_facts_from_entry"]
            )
            original = engine._facts_from_entry

            def different_device(entry, issues, task):
                facts = original(entry, issues, task)
                if facts is None or facts.path != external:
                    return facts
                current = facts.stat
                return FileFacts(
                    path=facts.path,
                    stat=SimpleNamespace(
                        st_mode=current.st_mode,
                        st_size=current.st_size,
                        st_blocks=current.st_blocks,
                        st_mtime=current.st_mtime,
                        st_dev=current.st_dev + 1,
                        st_ino=current.st_ino,
                        st_nlink=current.st_nlink,
                        st_uid=current.st_uid,
                        st_flags=getattr(current, "st_flags", 0),
                    ),
                )

            with mock.patch(
                "openclean.engine._facts_from_entry",
                side_effect=different_device,
            ), mock.patch(
                "openclean.analyzer.nonprivileged_action_block_reason",
                return_value="",
            ):
                analysis = analyze_path(root)

            self.assertTrue(analysis.complete)
            self.assertEqual(len(analysis.entries), 1)
            item = analysis.entries[0].item
            self.assertEqual(item.path, parent)
            self.assertEqual(item.size, local_file.stat().st_blocks * 512)
            self.assertEqual(item.cross_device_paths, 1)
            self.assertFalse(item.actionable)
            self.assertIn("跨卷", item.action_block_reason)
            issue = next(
                issue
                for issue in analysis.issues
                if issue.code == "cross_device_skipped"
            )
            self.assertEqual(issue.path, external)
            self.assertFalse(issue.blocking)

    def test_analyze_skips_same_device_different_filesystem(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            parent = root / "mounts"
            external = parent / "same-device-mount"
            external.mkdir(parents=True)
            local_file = parent / "local.bin"
            local_file.write_bytes(b"local")
            (external / "large.bin").write_bytes(b"x" * 100_000)

            def filesystem_id(path: Path) -> int:
                return 200 if path == external else 100

            with mock.patch(
                "openclean.engine.filesystem_id_retry",
                side_effect=filesystem_id,
            ), mock.patch(
                "openclean.analyzer.nonprivileged_action_block_reason",
                return_value="",
            ):
                analysis = analyze_path(root)

            self.assertTrue(analysis.complete)
            self.assertEqual(len(analysis.entries), 1)
            item = analysis.entries[0].item
            self.assertEqual(item.path, parent)
            self.assertEqual(item.size, local_file.stat().st_blocks * 512)
            self.assertEqual(item.cross_device_paths, 1)
            self.assertFalse(item.actionable)
            issue = next(
                issue
                for issue in analysis.issues
                if issue.code == "cross_device_skipped"
            )
            self.assertEqual(issue.path, external)

    def test_analyze_retries_interrupted_root_lstat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data.bin").write_bytes(b"data")
            original = Path.lstat
            interrupted = False

            def flaky_lstat(path: Path):
                nonlocal interrupted
                if path == root and not interrupted:
                    interrupted = True
                    raise InterruptedError(errno.EINTR, "interrupted")
                return original(path)

            with mock.patch.object(Path, "lstat", new=flaky_lstat):
                analysis = analyze_path(root)

            self.assertTrue(interrupted)
            self.assertTrue(analysis.complete)
            self.assertEqual(len(analysis.entries), 1)

    def test_analyze_retries_interrupted_filesystem_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            (root / "data" / "file.bin").write_bytes(b"data")
            original = os.statvfs
            interrupted = False

            def flaky_statvfs(path: str | os.PathLike[str]):
                nonlocal interrupted
                if Path(path) == root / "data" and not interrupted:
                    interrupted = True
                    raise InterruptedError(errno.EINTR, "interrupted")
                return original(path)

            with mock.patch("os.statvfs", side_effect=flaky_statvfs):
                analysis = analyze_path(root)

            self.assertTrue(interrupted)
            self.assertTrue(analysis.complete)
            self.assertEqual(len(analysis.entries), 1)

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
