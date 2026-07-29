from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openclean.cleanup import SelectionError, execute_cleanup, select_cleanup_items
from openclean.cli import main
from openclean.engine import (
    IgnoreRules,
    finalize_overlapping_result,
    scan_points,
)
from openclean.knowledge_base import KnowledgeBase
from openclean.scanpoints import DOMAINS, SYSTEM_JUNK, ScanPoint


class ScanPointContractTests(unittest.TestCase):
    def test_child_filters_require_child_expansion(self) -> None:
        with self.assertRaisesRegex(ValueError, "expand_children"):
            ScanPoint("invalid", (), child_globs=("*.log",))
        with self.assertRaisesRegex(ValueError, "expand_children"):
            ScanPoint("invalid", (), child_extensions=(".log",))

    def test_glob_and_extension_contract_rejects_unsafe_shapes(self) -> None:
        with self.assertRaisesRegex(ValueError, "递归"):
            ScanPoint("invalid", (), path_globs=("~/Library/**/Caches",))
        with self.assertRaisesRegex(ValueError, "带点扩展名"):
            ScanPoint(
                "invalid",
                (),
                expand_children=True,
                child_extensions=("log",),
            )

    def test_system_points_use_fine_grained_public_contracts(self) -> None:
        points = {point.category: point for point in SYSTEM_JUNK}

        self.assertTrue(points["用户缓存"].expand_children)
        self.assertEqual(points["用户缓存"].safety, "confirm")
        self.assertTrue(points["系统缓存"].requires_privilege)
        self.assertTrue(points["系统缓存"].expand_children)
        self.assertTrue(points["系统日志"].requires_privilege)
        self.assertTrue(points["系统诊断报告"].requires_privilege)
        self.assertTrue(points["系统迁移残留"].requires_privilege)
        self.assertEqual(
            points["沙盒容器缓存"].path_globs,
            ("~/Library/Containers/*/Data/Library/Caches",),
        )
        self.assertTrue(points["沙盒容器缓存"].expand_children)
        self.assertEqual(
            points["Darwin 用户缓存"].path_provider,
            "darwin-user-cache",
        )
        self.assertTrue(points["Darwin 用户缓存"].expand_children)
        self.assertEqual(
            points["诊断报告"].child_extensions,
            (".ips", ".crash", ".panic", ".diag", ".hang"),
        )
        for category in (
            "Xcode DerivedData",
            "Xcode 设备支持",
            "Xcode 文档缓存",
            "Xcode 设备日志",
            "Xcode Archives",
            "CoreSimulator 缓存",
        ):
            self.assertTrue(points[category].expand_children)
        self.assertEqual(
            points["失效启动项"].scanner,
            "broken-startup-items",
        )


class ScanPointExpansionTests(unittest.TestCase):
    def test_expand_children_reports_direct_items_instead_of_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Caches"
            first = root / "first"
            second = root / "second.bin"
            first.mkdir(parents=True)
            (first / "nested.bin").write_bytes(b"first")
            second.write_bytes(b"second")

            result = scan_points(
                [
                    ScanPoint(
                        "用户缓存",
                        (str(root),),
                        "confirm",
                        expand_children=True,
                    )
                ],
                workers=1,
            )

            self.assertTrue(result.complete)
            self.assertEqual(
                {item.path for item in result.items},
                {first, second},
            )
            self.assertNotIn(root, {item.path for item in result.items})
            self.assertTrue(all(item.identity is not None for item in result.items))
            self.assertTrue(all(not item.preselected for item in result.items))

    def test_child_glob_filters_direct_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Caches"
            selected = root / "cache-primary"
            skipped = root / "state"
            selected.mkdir(parents=True)
            skipped.mkdir()
            (selected / "data.bin").write_bytes(b"selected")
            (skipped / "data.bin").write_bytes(b"skipped")

            result = scan_points(
                [
                    ScanPoint(
                        "过滤缓存",
                        (str(root),),
                        expand_children=True,
                        child_globs=("cache-*",),
                    )
                ],
                workers=1,
            )

            self.assertEqual([item.path for item in result.items], [selected])

    def test_child_extensions_include_only_regular_supported_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp) / "DiagnosticReports"
            reports.mkdir()
            ips = reports / "app.ips"
            crash = reports / "APP.CRASH"
            unsupported = reports / "notes.txt"
            misleading_directory = reports / "folder.ips"
            ips.write_bytes(b"ips")
            crash.write_bytes(b"crash")
            unsupported.write_bytes(b"notes")
            misleading_directory.mkdir()
            (misleading_directory / "payload.bin").write_bytes(b"payload")

            result = scan_points(
                [
                    ScanPoint(
                        "诊断报告",
                        (str(reports),),
                        expand_children=True,
                        child_extensions=(".ips", ".crash"),
                    )
                ],
                workers=1,
            )

            self.assertEqual(
                {item.path for item in result.items},
                {ips, crash},
            )

    def test_path_glob_does_not_traverse_symlinked_components(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            containers = root / "Containers"
            cache = containers / "com.example.app" / "Data" / "Library" / "Caches"
            item = cache / "CacheData"
            item.mkdir(parents=True)
            (item / "data.bin").write_bytes(b"inside")

            outside = root / "outside"
            escaped = outside / "Data" / "Library" / "Caches" / "escaped"
            escaped.mkdir(parents=True)
            (escaped / "secret.bin").write_bytes(b"outside")
            containers.mkdir(exist_ok=True)
            (containers / "linked").symlink_to(outside, target_is_directory=True)

            result = scan_points(
                [
                    ScanPoint(
                        "沙盒容器缓存",
                        (),
                        path_globs=(
                            str(containers / "*" / "Data" / "Library" / "Caches"),
                        ),
                        expand_children=True,
                    )
                ],
                workers=1,
            )

            self.assertEqual([candidate.path for candidate in result.items], [item])
            self.assertNotIn(escaped, {candidate.path for candidate in result.items})

    def test_protected_expanded_child_is_gated_before_lstat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Caches"
            protected = root / "protected"
            counted = root / "counted"
            protected.mkdir(parents=True)
            counted.mkdir()
            (protected / "secret.bin").write_bytes(b"secret")
            (counted / "data.bin").write_bytes(b"counted")
            knowledge_base = KnowledgeBase.from_mapping(
                {
                    "schema_version": 1,
                    "protect": {"paths": [str(protected)]},
                }
            )
            original_lstat = Path.lstat

            def guarded_lstat(path: Path):
                if path == protected:
                    raise AssertionError("受保护候选不应执行 lstat")
                return original_lstat(path)

            with mock.patch.object(Path, "lstat", new=guarded_lstat):
                result = scan_points(
                    [
                        ScanPoint(
                            "用户缓存",
                            (str(root),),
                            expand_children=True,
                        )
                    ],
                    ignore=IgnoreRules(knowledge_base=knowledge_base),
                    workers=1,
                )

            self.assertEqual([item.path for item in result.items], [counted])

    def test_cleanup_of_expanded_candidate_preserves_sibling_and_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = home / "Library" / "Caches"
            selected = root / "selected"
            sibling = root / "sibling"
            selected.mkdir(parents=True)
            sibling.mkdir()
            (selected / "data.bin").write_bytes(b"selected")
            (sibling / "data.bin").write_bytes(b"sibling")
            result = scan_points(
                [
                    ScanPoint(
                        "用户缓存",
                        (str(root),),
                        expand_children=True,
                    )
                ],
                workers=1,
            )
            selected_item = next(
                item for item in result.items if item.path == selected
            )

            report = execute_cleanup(
                [selected_item],
                IgnoreRules(),
                home=home,
            )

            self.assertTrue(report.complete)
            self.assertTrue(root.is_dir())
            self.assertFalse(selected.exists())
            self.assertTrue(sibling.is_dir())
            self.assertTrue((home / ".Trash" / "selected").is_dir())

    def test_privileged_scan_point_is_visible_but_never_selectable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "LibraryCaches"
            child = root / "system-cache"
            child.mkdir(parents=True)
            (child / "data.bin").write_bytes(b"data")

            result = scan_points(
                [
                    ScanPoint(
                        "系统缓存",
                        (str(root),),
                        "confirm",
                        expand_children=True,
                        requires_privilege=True,
                    )
                ],
                workers=1,
            )

            self.assertEqual(len(result.items), 1)
            item = result.items[0]
            self.assertTrue(item.requires_privilege)
            self.assertFalse(item.actionable)
            self.assertFalse(item.preselected)
            self.assertIn("特权帮助器", item.action_block_reason)
            with self.assertRaisesRegex(SelectionError, "特权帮助器"):
                select_cleanup_items(
                    result.items,
                    selectors=[str(child)],
                    include_confirm=True,
                )

    def test_clean_json_exposes_privileged_readonly_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_root = root / "LibraryCaches"
            child = cache_root / "system-cache"
            child.mkdir(parents=True)
            (child / "data.bin").write_bytes(b"data")
            rules = root / "rules.json"
            rules.write_text(
                json.dumps({"schema_version": 1}),
                encoding="utf-8",
            )
            point = ScanPoint(
                "系统缓存",
                (str(cache_root),),
                "confirm",
                expand_children=True,
                requires_privilege=True,
            )
            stdout = io.StringIO()

            with mock.patch.dict(DOMAINS, {"system": [point]}), \
                    contextlib.redirect_stdout(stdout):
                status = main(
                    ["clean", "junk", "--rules", str(rules), "--json"]
                )

            payload = json.loads(stdout.getvalue())
            item = payload["categories"][0]["items"][0]
            self.assertEqual(status, 0)
            self.assertTrue(item["requires_privilege"])
            self.assertFalse(item["actionable"])
            self.assertFalse(item["preselected"])


class FineGrainedOverlapTests(unittest.TestCase):
    def _points(self, logs: Path) -> list[ScanPoint]:
        return [
            ScanPoint(
                "用户日志",
                (str(logs),),
                "confirm",
                expand_children=True,
                domain="system",
            ),
            ScanPoint(
                "诊断报告",
                (str(logs / "DiagnosticReports"),),
                expand_children=True,
                child_extensions=(".ips", ".crash", ".panic", ".diag", ".hang"),
                domain="system",
            ),
        ]

    def test_overlap_is_owned_by_specific_item_without_double_counting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp) / "Logs"
            reports = logs / "DiagnosticReports"
            app_logs = logs / "Example"
            reports.mkdir(parents=True)
            app_logs.mkdir()
            crash = reports / "app.ips"
            unsupported = reports / "notes.txt"
            ordinary = app_logs / "app.log"
            crash.write_bytes(b"crash")
            unsupported.write_bytes(b"notes")
            ordinary.write_bytes(b"ordinary")

            raw = scan_points(self._points(logs), workers=1)
            result = finalize_overlapping_result(raw)

            expected = sum(
                path.stat().st_blocks * 512
                for path in (crash, unsupported, ordinary)
            )
            self.assertEqual(result.total, expected)
            by_path = {item.path: item for item in result.items}
            self.assertEqual(by_path[crash].category, "诊断报告")
            self.assertTrue(by_path[crash].preselected)
            residual = by_path[reports]
            self.assertEqual(residual.category, "用户日志")
            self.assertFalse(residual.actionable)
            self.assertEqual(
                residual.action_block_reason,
                "与更具体的清理分类重叠",
            )

    def test_scan_cli_applies_overlap_finalization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / "Logs"
            reports = logs / "DiagnosticReports"
            reports.mkdir(parents=True)
            crash = reports / "app.ips"
            crash.write_bytes(b"crash")
            rules = root / "rules.json"
            rules.write_text(
                json.dumps({"schema_version": 1}),
                encoding="utf-8",
            )
            stdout = io.StringIO()

            with mock.patch.dict(DOMAINS, {"system": self._points(logs)}), \
                    contextlib.redirect_stdout(stdout):
                status = main(
                    [
                        "scan",
                        "--domain",
                        "system",
                        "--rules",
                        str(rules),
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(status, 0)
            self.assertEqual(payload["total_bytes"], crash.stat().st_blocks * 512)
            self.assertEqual(len(payload["items"]), 1)
            self.assertEqual(payload["items"][0]["category"], "诊断报告")


if __name__ == "__main__":
    unittest.main()
