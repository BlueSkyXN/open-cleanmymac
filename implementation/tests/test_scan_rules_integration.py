from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openclean.cli import main
from openclean.engine import IgnoreRules, scan_points, scan_project_artifacts
from openclean.knowledge_base import KnowledgeBase
from openclean.models import FileFacts
from openclean.scanpoints import ScanPoint


class ScanRulesIntegrationTests(unittest.TestCase):
    def test_knowledge_base_gate_runs_before_file_stat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            protected = Path(tmp) / "protected"
            protected.mkdir()
            knowledge_base = KnowledgeBase.from_mapping(
                {
                    "schema_version": 1,
                    "protect": {"paths": [str(protected)]},
                }
            )

            with mock.patch.object(
                Path, "lstat", side_effect=AssertionError("不应访问受保护路径")
            ):
                result = scan_points(
                    [ScanPoint("保护测试", (str(protected),))],
                    ignore=IgnoreRules(knowledge_base=knowledge_base),
                    workers=1,
                )

            self.assertTrue(result.complete)
            self.assertEqual(result.items, [])

    def test_task_failure_is_reported_as_incomplete(self) -> None:
        class ExplodingGate:
            def should_ignore(self, item: FileFacts) -> bool:
                raise RuntimeError("policy failed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "file.bin").write_bytes(b"content")

            result = scan_points(
                [ScanPoint("测试缓存", (str(root),))],
                ignore=ExplodingGate(),
                workers=1,
            )

            self.assertFalse(result.complete)
            self.assertEqual(result.items, [])
            self.assertEqual(result.issues[0].code, "task_failed")
            self.assertEqual(result.issues[0].task, "测试缓存")

    def test_external_symlink_ancestor_is_rejected_during_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            target = base / "target"
            cache = target / "cache"
            cache.mkdir(parents=True)
            (cache / "data.bin").write_bytes(b"data")
            alias = base / "alias"
            alias.symlink_to(target, target_is_directory=True)

            result = scan_points(
                [ScanPoint("外部路径", (str(alias / "cache"),))],
                workers=1,
            )

            self.assertEqual(result.items, [])
            self.assertEqual(result.issues[0].code, "unsafe_symlink_ancestor")

    def test_protected_descendant_is_excluded_from_directory_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "cache"
            protected = root / "protected"
            protected.mkdir(parents=True)
            counted = root / "counted.bin"
            counted.write_bytes(b"12345")
            (protected / "secret.bin").write_bytes(b"x" * 20)

            knowledge_base = KnowledgeBase.from_mapping(
                {
                    "schema_version": 1,
                    "protect": {"paths": [str(protected)]},
                }
            )
            result = scan_points(
                [ScanPoint("测试缓存", (str(root),))],
                ignore=IgnoreRules(knowledge_base=knowledge_base),
                workers=1,
            )

            self.assertEqual(result.total, counted.stat().st_blocks * 512)
            self.assertEqual(len(result.items), 1)

    def test_protected_project_artifact_is_not_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            artifact = project / "node_modules"
            artifact.mkdir(parents=True)
            (project / "package.json").write_text("{}", encoding="utf-8")
            (artifact / "package.bin").write_bytes(b"content")
            knowledge_base = KnowledgeBase.from_mapping(
                {
                    "schema_version": 1,
                    "protect": {"paths": [str(artifact)]},
                }
            )

            result = scan_project_artifacts(
                [project], ignore=IgnoreRules(knowledge_base=knowledge_base)
            )

            self.assertEqual(result.items, [])

    def test_cli_rules_option_and_empty_json_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "project" / "node_modules"
            artifact.mkdir(parents=True)
            (artifact / "package.bin").write_bytes(b"content")
            rules = root / "rules.json"
            rules.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "protect": {"paths": [str(artifact)]},
                    }
                ),
                encoding="utf-8",
            )
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                status = main(
                    [
                        "scan",
                        "--domain",
                        "project",
                        "--project-root",
                        str(root),
                        "--rules",
                        str(rules),
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(status, 0)
            self.assertEqual(payload["schema_version"], 2)
            self.assertEqual(payload["command"], "scan")
            self.assertEqual(payload["mode"], "report")
            self.assertEqual(payload["requested_domains"], ["project"])
            self.assertTrue(payload["complete"])
            self.assertEqual(payload["items"], [])
            self.assertEqual(payload["total_bytes"], 0)

    def test_cli_legacy_ignore_remains_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "project" / "node_modules"
            artifact.mkdir(parents=True)
            (artifact / "package.bin").write_bytes(b"content")
            rules = root / "rules.json"
            rules.write_text(
                json.dumps({"schema_version": 1}), encoding="utf-8"
            )
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                status = main(
                    [
                        "scan",
                        "--domain",
                        "project",
                        "--project-root",
                        str(root),
                        "--rules",
                        str(rules),
                        "--ignore",
                        "node_modules",
                        "--json",
                    ]
                )

            self.assertEqual(status, 0)
            self.assertEqual(json.loads(stdout.getvalue())["items"], [])

    def test_cli_reports_invalid_rules_without_scanning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rules = Path(tmp) / "rules.json"
            rules.write_text("not-json", encoding="utf-8")
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                status = main(
                    ["scan", "--domain", "project", "--rules", str(rules)]
                )

            self.assertEqual(status, 2)
            self.assertIn("规则加载失败", stderr.getvalue())

    def test_cli_reports_invalid_rules_as_json_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rules = Path(tmp) / "rules.json"
            rules.write_text("not-json", encoding="utf-8")
            stdout = io.StringIO()
            stderr = io.StringIO()

            with contextlib.redirect_stdout(stdout), \
                    contextlib.redirect_stderr(stderr):
                status = main(
                    ["scan", "--rules", str(rules), "--json"]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(status, 2)
            self.assertEqual(payload["status"], "error")
            self.assertEqual(payload["error"]["code"], "rules_error")
            self.assertFalse(payload["executed"])
            self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
