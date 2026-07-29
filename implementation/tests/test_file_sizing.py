from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from openclean.cli import main
from openclean.engine import scan_points
from openclean.models import FileFacts
from openclean.scanpoints import DOMAINS, ScanPoint


def _stat_with_blocks(stat_result, blocks: int):
    return SimpleNamespace(
        st_mode=stat_result.st_mode,
        st_size=stat_result.st_size,
        st_blocks=blocks,
        st_mtime=stat_result.st_mtime,
        st_dev=stat_result.st_dev,
        st_ino=stat_result.st_ino,
        st_nlink=stat_result.st_nlink,
        st_uid=stat_result.st_uid,
    )


class FileSizingTests(unittest.TestCase):
    def test_cloud_heuristic_requires_positive_logical_and_zero_blocks(self) -> None:
        path = Path("/tmp/cloud-placeholder")
        cloud = FileFacts(path, SimpleNamespace(st_size=10, st_blocks=0))
        empty = FileFacts(path, SimpleNamespace(st_size=0, st_blocks=0))
        unknown = FileFacts(path, SimpleNamespace(st_size=10))

        self.assertTrue(cloud.is_probable_cloud_placeholder)
        self.assertFalse(empty.is_probable_cloud_placeholder)
        self.assertFalse(unknown.is_probable_cloud_placeholder)

    def test_uses_allocated_blocks_and_keeps_logical_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "cache"
            root.mkdir()
            payload = root / "payload.bin"
            payload.write_bytes(b"logical-content")

            result = scan_points(
                [ScanPoint("测试缓存", (str(root),))], workers=1
            )

            expected_allocated = payload.stat().st_blocks * 512
            self.assertEqual(result.total, expected_allocated)
            self.assertEqual(result.items[0].size, expected_allocated)
            self.assertEqual(
                result.items[0].logical_size,
                len(b"logical-content"),
            )
            self.assertEqual(
                result.items[0].allocated_size,
                expected_allocated,
            )

    def test_cloud_placeholder_is_visible_but_not_reclaimable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cloud = Path(tmp) / "remote.bin"
            cloud.write_bytes(b"remote-content")
            real_stat = cloud.lstat()
            original_lstat = Path.lstat

            def fake_lstat(path: Path):
                stat_result = original_lstat(path)
                if path == cloud:
                    return _stat_with_blocks(real_stat, 0)
                return stat_result

            with mock.patch.object(Path, "lstat", new=fake_lstat):
                result = scan_points(
                    [ScanPoint("云文件测试", (str(cloud),))], workers=1
                )

            self.assertTrue(result.complete)
            self.assertEqual(result.total, 0)
            self.assertEqual(len(result.items), 1)
            item = result.items[0]
            self.assertEqual(item.size, 0)
            self.assertEqual(item.logical_size, len(b"remote-content"))
            self.assertEqual(item.allocated_size, 0)
            self.assertTrue(item.is_cloud_file)
            self.assertEqual(item.cloud_file_count, 1)
            self.assertEqual(
                item.cloud_logical_size,
                len(b"remote-content"),
            )
            self.assertEqual(item.safety, "critical")
            self.assertFalse(item.preselected)
            self.assertIn("不计入可回收容量", item.note)

    def test_mixed_directory_json_excludes_cloud_bytes_and_blocks_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "cache"
            cache.mkdir()
            local_file = cache / "local.bin"
            cloud_file = cache / "remote.bin"
            local_file.write_bytes(b"local")
            cloud_file.write_bytes(b"remote" * 100)
            cloud_real_stat = cloud_file.lstat()
            original_lstat = Path.lstat
            rules = root / "rules.json"
            rules.write_text(
                json.dumps({"schema_version": 1}), encoding="utf-8"
            )

            def fake_lstat(path: Path):
                stat_result = original_lstat(path)
                if path == cloud_file:
                    return _stat_with_blocks(cloud_real_stat, 0)
                return stat_result

            stdout = io.StringIO()
            with mock.patch.object(Path, "lstat", new=fake_lstat), mock.patch.dict(
                DOMAINS,
                {"developer": [ScanPoint("混合缓存", (str(cache),))]},
            ), contextlib.redirect_stdout(stdout):
                status = main(
                    ["clean", "dev", "--rules", str(rules), "--json"]
                )

            payload = json.loads(stdout.getvalue())
            item = payload["categories"][0]["items"][0]
            local_allocated = local_file.stat().st_blocks * 512
            self.assertEqual(status, 0)
            self.assertEqual(payload["total_bytes"], local_allocated)
            self.assertEqual(payload["potential_bytes"], local_allocated)
            self.assertEqual(payload["reclaimable_bytes"], 0)
            self.assertEqual(item["potential_bytes"], local_allocated)
            self.assertEqual(item["reclaimable_bytes"], 0)
            self.assertEqual(item["allocated_bytes"], local_allocated)
            self.assertEqual(
                item["logical_bytes"],
                len(b"local") + len(b"remote" * 100),
            )
            self.assertEqual(item["cloud_file_count"], 1)
            self.assertEqual(
                item["cloud_logical_bytes"], len(b"remote" * 100)
            )
            self.assertEqual(item["safety"], "critical")
            self.assertFalse(item["preselected"])

    def test_hard_links_are_deduplicated_for_both_size_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "cache"
            root.mkdir()
            original = root / "original.bin"
            linked = root / "linked.bin"
            original.write_bytes(b"hard-link-content")
            linked.hardlink_to(original)

            result = scan_points(
                [ScanPoint("硬链接测试", (str(root),))], workers=1
            )

            item = result.items[0]
            self.assertEqual(item.logical_size, len(b"hard-link-content"))
            self.assertEqual(
                item.allocated_size,
                original.stat().st_blocks * 512,
            )


if __name__ == "__main__":
    unittest.main()
