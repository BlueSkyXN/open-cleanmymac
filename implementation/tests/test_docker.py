from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openclean.cli import main
from openclean.docker import (
    DockerPruneError,
    parse_docker_size,
    prune_docker_resource,
    scan_docker_resources,
)
from openclean.engine import scan_domains
from openclean.models import Item, ScanResult
from openclean.scanpoints import DOMAINS, ScanPoint


def _docker_row(
    resource_type: str,
    total: str,
    active: str,
    size: str,
    reclaimable: str,
) -> str:
    return json.dumps(
        {
            "Type": resource_type,
            "TotalCount": total,
            "Active": active,
            "Size": size,
            "Reclaimable": reclaimable,
        }
    )


class DockerScannerTests(unittest.TestCase):
    def test_parses_documented_decimal_and_binary_sizes(self) -> None:
        self.assertEqual(parse_docker_size("158B"), 158)
        self.assertEqual(parse_docker_size("1.114kB (49%)"), 1_114)
        self.assertEqual(parse_docker_size("256.5MB (100%)"), 256_500_000)
        self.assertEqual(parse_docker_size("2.498GB (94%)"), 2_498_000_000)
        self.assertEqual(parse_docker_size("1.5 GiB"), 1_610_612_736)

    def test_missing_cli_is_a_supported_empty_result(self) -> None:
        runner = mock.Mock(side_effect=AssertionError("不应启动子进程"))

        result = scan_docker_resources(
            finder=lambda _: None,
            runner=runner,
        )

        self.assertTrue(result.complete)
        self.assertEqual(result.items, [])
        self.assertEqual(result.issues, [])
        runner.assert_not_called()

    def test_maps_official_json_lines_to_conservative_resource_items(self) -> None:
        stdout = "\n".join(
            (
                _docker_row("Images", "6", "2", "2.631GB", "2.498GB (94%)"),
                _docker_row("Containers", "7", "1", "2.23kB", "1.114kB (49%)"),
                _docker_row(
                    "Local Volumes", "1", "0", "256.5MB", "256.5MB (100%)"
                ),
                _docker_row("Build Cache", "17", "0", "158B", "158B"),
            )
        )
        calls: list[tuple[list[str], dict[str, object]]] = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, stdout, "")

        result = scan_docker_resources(
            finder=lambda _: "/usr/local/bin/docker",
            runner=runner,
        )

        self.assertTrue(result.complete)
        self.assertEqual(
            calls[0][0],
            [
                "/usr/local/bin/docker",
                "system",
                "df",
                "--format",
                "json",
            ],
        )
        self.assertTrue(calls[0][1]["capture_output"])
        by_identifier = {item.identifier: item for item in result.items}
        self.assertEqual(
            result.total,
            2_498_000_000 + 1_114 + 256_500_000 + 158,
        )
        self.assertIsNone(by_identifier["docker:images"].path)
        self.assertEqual(by_identifier["docker:images"].resource_kind, "docker")
        self.assertEqual(
            by_identifier["docker:images"].resource_total_size,
            2_631_000_000,
        )
        self.assertEqual(by_identifier["docker:images"].total_count, 6)
        self.assertEqual(by_identifier["docker:images"].active_count, 2)
        self.assertEqual(by_identifier["docker:images"].safety, "confirm")
        self.assertFalse(by_identifier["docker:images"].preselected)
        self.assertTrue(by_identifier["docker:images"].actionable)
        self.assertTrue(by_identifier["docker:containers"].actionable)
        self.assertEqual(
            by_identifier["docker:local-volumes"].safety,
            "critical",
        )
        self.assertFalse(by_identifier["docker:local-volumes"].preselected)
        self.assertFalse(by_identifier["docker:local-volumes"].actionable)
        self.assertIn(
            "不支持自动清理",
            by_identifier["docker:local-volumes"].action_block_reason,
        )
        self.assertEqual(by_identifier["docker:build-cache"].safety, "safe")
        self.assertTrue(by_identifier["docker:build-cache"].preselected)
        self.assertTrue(by_identifier["docker:build-cache"].actionable)

    def test_prune_maps_only_audited_resource_identifiers(self) -> None:
        expected = {
            "docker:build-cache": [
                "/usr/local/bin/docker",
                "builder",
                "prune",
                "--all",
                "--force",
            ],
            "docker:images": [
                "/usr/local/bin/docker",
                "image",
                "prune",
                "--all",
                "--force",
            ],
            "docker:containers": [
                "/usr/local/bin/docker",
                "container",
                "prune",
                "--force",
            ],
        }
        for identifier, expected_command in expected.items():
            with self.subTest(identifier=identifier):
                calls: list[list[str]] = []

                def runner(command, _calls=calls, **_):
                    _calls.append(command)
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        "Total reclaimed space: 1.5 MB\n",
                        "",
                    )

                result = prune_docker_resource(
                    identifier,
                    finder=lambda _: "/usr/local/bin/docker",
                    runner=runner,
                )

                self.assertEqual(calls, [expected_command])
                self.assertEqual(result.reclaimed_bytes, 1_500_000)
                self.assertIn("prune 已完成", result.message)

        with self.assertRaisesRegex(DockerPruneError, "不支持自动清理"):
            prune_docker_resource("docker:local-volumes")
        with self.assertRaisesRegex(DockerPruneError, "不支持自动清理"):
            prune_docker_resource("docker:unknown")

    def test_prune_failure_modes_are_explicit_and_never_guess_reclaimed_size(self) -> None:
        with self.assertRaisesRegex(DockerPruneError, "未找到 Docker CLI"):
            prune_docker_resource(
                "docker:build-cache",
                finder=lambda _: None,
            )

        with self.assertRaisesRegex(DockerPruneError, "daemon unavailable"):
            prune_docker_resource(
                "docker:build-cache",
                finder=lambda _: "/usr/local/bin/docker",
                runner=lambda command, **_: subprocess.CompletedProcess(
                    command, 1, "", "daemon unavailable"
                ),
            )

        result = prune_docker_resource(
            "docker:build-cache",
            finder=lambda _: "/usr/local/bin/docker",
            runner=lambda command, **_: subprocess.CompletedProcess(
                command, 0, "prune complete without size", ""
            ),
        )
        self.assertEqual(result.reclaimed_bytes, 0)
        self.assertIn("未提供可解析", result.message)

    def test_zero_reclaimable_rows_are_not_candidates(self) -> None:
        stdout = _docker_row("Images", "1", "1", "20MB", "0B (0%)")

        result = scan_docker_resources(
            finder=lambda _: "/usr/local/bin/docker",
            runner=lambda command, **_: subprocess.CompletedProcess(
                command, 0, stdout, ""
            ),
        )

        self.assertTrue(result.complete)
        self.assertEqual(result.items, [])

    def test_daemon_failure_is_a_structured_partial_result(self) -> None:
        def runner(command, **_):
            return subprocess.CompletedProcess(
                command,
                1,
                "",
                "Cannot connect to the Docker daemon",
            )

        result = scan_docker_resources(
            finder=lambda _: "/usr/local/bin/docker",
            runner=runner,
        )

        self.assertFalse(result.complete)
        self.assertEqual(result.items, [])
        self.assertEqual(result.issues[0].code, "tool_unavailable")
        self.assertEqual(result.issues[0].task, "docker-system-df")
        self.assertIn("Cannot connect", result.issues[0].message)

    def test_timeout_is_a_structured_partial_result(self) -> None:
        def runner(command, **_):
            raise subprocess.TimeoutExpired(command, 0.01)

        result = scan_docker_resources(
            finder=lambda _: "/usr/local/bin/docker",
            runner=runner,
            timeout=0.01,
        )

        self.assertFalse(result.complete)
        self.assertEqual(result.issues[0].code, "tool_unavailable")
        self.assertIn("0.01 秒", result.issues[0].message)

    def test_invalid_line_does_not_discard_valid_rows(self) -> None:
        stdout = "\n".join(
            (
                _docker_row("Build Cache", "1", "0", "1MB", "500kB (50%)"),
                "{not-json",
            )
        )

        result = scan_docker_resources(
            finder=lambda _: "/usr/local/bin/docker",
            runner=lambda command, **_: subprocess.CompletedProcess(
                command, 0, stdout, ""
            ),
        )

        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].size, 500_000)
        self.assertFalse(result.complete)
        self.assertEqual(result.issues[0].code, "tool_output_invalid")
        self.assertIn("第 2 行", result.issues[0].message)

    def test_clean_dev_serializes_virtual_resources_without_fake_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "cache"
            cache.mkdir()
            cache_file = cache / "data.bin"
            cache_file.write_bytes(b"cache")
            rules = root / "rules.json"
            rules.write_text(
                json.dumps({"schema_version": 1}), encoding="utf-8"
            )
            points = [
                ScanPoint("文件缓存", (str(cache),)),
                ScanPoint("Docker 资源", (), scanner="docker"),
            ]
            docker_result = ScanResult(
                items=[
                    Item(
                        path=None,
                        size=1_000,
                        category="Docker 构建缓存",
                        preselected=True,
                        domain="developer",
                        resource_kind="docker",
                        identifier="docker:build-cache",
                        resource_total_size=2_000,
                        total_count=3,
                        active_count=0,
                    )
                ]
            )
            stdout = io.StringIO()

            with mock.patch.dict(DOMAINS, {"developer": points}), mock.patch(
                "openclean.engine.scan_docker_resources",
                return_value=docker_result,
            ) as scanner, contextlib.redirect_stdout(stdout):
                status = main(
                    ["clean", "dev", "--rules", str(rules), "--json"]
                )

            payload = json.loads(stdout.getvalue())
            items = payload["categories"][0]["items"]
            docker_item = next(
                item for item in items if item["resource_kind"] == "docker"
            )
            self.assertEqual(status, 0)
            scanner.assert_called_once_with()
            self.assertIsNone(docker_item["path"])
            self.assertEqual(docker_item["identifier"], "docker:build-cache")
            self.assertEqual(docker_item["resource_total_bytes"], 2_000)
            self.assertEqual(
                payload["total_bytes"],
                cache_file.stat().st_blocks * 512 + 1_000,
            )

    def test_dynamic_scanner_only_runs_when_domain_declares_it(self) -> None:
        with mock.patch.dict(
            DOMAINS,
            {"developer": [ScanPoint("测试", ())]},
        ), mock.patch("openclean.engine.scan_docker_resources") as scanner:
            result = scan_domains(["developer"], workers=1)

        self.assertTrue(result.complete)
        scanner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
