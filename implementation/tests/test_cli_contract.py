from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openclean.cli import main
from openclean.models import Item, ScanResult


def _rules(root: Path) -> Path:
    path = root / "rules.json"
    path.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    return path


class CliContractTests(unittest.TestCase):
    def test_help_is_stdout_only_and_explains_safety_contracts(self) -> None:
        cases = (
            (("--help",), "macOS 清理工具"),
            (("scan", "--help"), "仅本次运行"),
            (("clean", "--help"), "永久操作"),
            (("purge", "--help"), "同卷 Trash"),
            (("analyze", "--help"), "当前层级"),
            (("optimize", "--help"), "purgeable"),
            (("optimize", "ram", "--help"), "退出码 1"),
            (("optimize", "purgeable", "--help"), "退出码 1"),
            (("ignore", "--help"), "持久忽略路径"),
            (("ignore", "list", "--help"), "规则文件"),
            (("ignore", "add", "--help"), "规范化绝对路径"),
            (("ignore", "remove", "--help"), "精确匹配"),
            (("config", "--help"), "analytics"),
            (("cat", "--help"), "输出 JSON"),
        )

        for arguments, expected in cases:
            with self.subTest(arguments=arguments):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with contextlib.redirect_stdout(stdout), \
                        contextlib.redirect_stderr(stderr), \
                        self.assertRaises(SystemExit) as raised:
                    main(list(arguments))

                self.assertEqual(raised.exception.code, 0)
                self.assertIn(expected, stdout.getvalue())
                self.assertEqual(stderr.getvalue(), "")

    def test_scan_deduplicates_domains_and_explicit_project_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_root = root / "projects"
            project_root.mkdir()
            rules = _rules(root)
            stdout = io.StringIO()

            with mock.patch(
                "openclean.cli.scan_domains", return_value=ScanResult()
            ) as scan_domains, mock.patch(
                "openclean.cli.scan_project_artifacts",
                return_value=ScanResult(),
            ) as scan_projects, contextlib.redirect_stdout(stdout):
                status = main(
                    [
                        "scan",
                        "--domain",
                        "developer",
                        "--domain",
                        "developer",
                        "--domain",
                        "project",
                        "--project-root",
                        str(project_root),
                        "--project-root",
                        str(project_root),
                        "--rules",
                        str(rules),
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(status, 0)
            self.assertEqual(payload["requested_domains"], ["developer", "project"])
            self.assertEqual(scan_domains.call_args.args[0], ["developer"])
            self.assertEqual(scan_projects.call_args.args[0], [project_root])

    def test_project_root_requires_project_domain(self) -> None:
        stdout = io.StringIO()
        with mock.patch("openclean.cli.scan_domains") as scan_domains, mock.patch(
            "openclean.cli.scan_project_artifacts"
        ) as scan_projects, contextlib.redirect_stdout(stdout):
            status = main(
                [
                    "scan",
                    "--domain",
                    "developer",
                    "--project-root",
                    "/definitely/missing",
                    "--json",
                ]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(status, 2)
        self.assertEqual(
            payload["error"]["code"], "invalid_option_combination"
        )
        scan_domains.assert_not_called()
        scan_projects.assert_not_called()

    def test_explicit_project_root_must_be_real_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            file_path = root / "file"
            file_path.write_text("not a directory", encoding="utf-8")
            real_directory = root / "real"
            real_directory.mkdir()
            symlink = root / "link"
            symlink.symlink_to(real_directory, target_is_directory=True)
            cases = (
                (root / "missing", "不存在"),
                (file_path, "不是目录"),
                (symlink, "符号链接"),
            )

            for candidate, message in cases:
                with self.subTest(candidate=candidate):
                    stdout = io.StringIO()
                    with mock.patch(
                        "openclean.cli.scan_project_artifacts"
                    ) as scanner, contextlib.redirect_stdout(stdout):
                        status = main(
                            [
                                "scan",
                                "--domain",
                                "project",
                                "--project-root",
                                str(candidate),
                                "--json",
                            ]
                        )

                    payload = json.loads(stdout.getvalue())
                    self.assertEqual(status, 2)
                    self.assertEqual(
                        payload["error"]["code"], "invalid_project_root"
                    )
                    self.assertIn(message, payload["error"]["message"])
                    scanner.assert_not_called()

    def test_text_report_explains_exact_and_blocked_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rules = _rules(root)
            exact = Item(
                path=root / "exact",
                size=100,
                category="环境缓存",
                safety="confirm",
                domain="developer",
                requires_explicit_selection=True,
            )
            blocked = Item(
                path=root / "blocked",
                size=200,
                category="系统缓存",
                safety="critical",
                domain="developer",
                actionable=False,
                requires_privilege=True,
                action_block_reason="需要尚未实现的特权帮助器",
            )
            stdout = io.StringIO()

            with mock.patch(
                "openclean.cli.scan_domains",
                return_value=ScanResult(items=[exact, blocked]),
            ), contextlib.redirect_stdout(stdout):
                status = main(
                    [
                        "scan",
                        "--domain",
                        "developer",
                        "--rules",
                        str(rules),
                    ]
                )

            output = stdout.getvalue()
            self.assertEqual(status, 0)
            self.assertIn("[需 --select 精确选择]", output)
            self.assertIn("[需要特权 helper]", output)
            self.assertIn("[不可执行: 需要尚未实现的特权帮助器]", output)

    def test_unsupported_source_runtime_fails_before_scanning(self) -> None:
        stdout = io.StringIO()
        with mock.patch(
            "openclean.cli.sys.version_info", (3, 9, 6)
        ), mock.patch("openclean.cli.scan_domains") as scanner, \
                contextlib.redirect_stdout(stdout):
            status = main(["scan", "--domain", "developer", "--json"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(status, 2)
        self.assertEqual(payload["error"]["code"], "unsupported_python")
        self.assertIn("3.11", payload["error"]["message"])
        self.assertIn("3.9.6", payload["error"]["message"])
        scanner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
