from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openclean.cleanup import select_cleanup_items
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


_DOCKER_BINARY = os.path.realpath("/tmp/openclean-test-docker")
_DOCKER_CONTEXT = "desktop-linux"
_DOCKER_ENDPOINT = "unix:///Users/example/.docker/run/docker.sock"
_DOCKER_DAEMON_ID = "DAEMON:A"


def _docker_binding(
    *,
    daemon_id: str = _DOCKER_DAEMON_ID,
    endpoint: str = _DOCKER_ENDPOINT,
) -> str:
    return json.dumps(
        {
            "v": 2,
            "kind": "docker",
            "cli_path": _DOCKER_BINARY,
            "context_name": _DOCKER_CONTEXT,
            "target": {"kind": "context", "value": _DOCKER_CONTEXT},
            "endpoint_host": endpoint,
            "skip_tls_verify": False,
            "daemon_id": daemon_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _target_aware_runner(
    *,
    df_stdout: str | None = None,
    prune=None,
    calls: list[list[str]] | None = None,
    daemon_id: str = _DOCKER_DAEMON_ID,
):
    def runner(command, **kwargs):
        if calls is not None:
            calls.append(command)
        if command == [_DOCKER_BINARY, "context", "show"]:
            return subprocess.CompletedProcess(
                command, 0, f"{_DOCKER_CONTEXT}\n", ""
            )
        if command == [
            _DOCKER_BINARY,
            "context",
            "inspect",
            _DOCKER_CONTEXT,
        ]:
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps([{
                    "Name": _DOCKER_CONTEXT,
                    "Endpoints": {
                        "docker": {
                            "Host": _DOCKER_ENDPOINT,
                            "SkipTLSVerify": False,
                        }
                    },
                }]),
                "",
            )
        if command == [
            _DOCKER_BINARY,
            "--context",
            _DOCKER_CONTEXT,
            "info",
            "--format",
            "{{json .ID}}",
        ]:
            return subprocess.CompletedProcess(
                command, 0, json.dumps(daemon_id), ""
            )
        if command == [
            _DOCKER_BINARY,
            "--context",
            _DOCKER_CONTEXT,
            "system",
            "df",
            "--format",
            "json",
        ] and df_stdout is not None:
            return subprocess.CompletedProcess(command, 0, df_stdout, "")
        if prune is not None:
            return prune(command, **kwargs)
        raise AssertionError(f"unexpected command: {command}")

    return runner


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
        bound_runner = _target_aware_runner(df_stdout=stdout)

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return bound_runner(command, **kwargs)

        result = scan_docker_resources(
            finder=lambda _: _DOCKER_BINARY,
            runner=runner,
        )

        self.assertTrue(result.complete)
        self.assertEqual(
            calls[3][0],
            [
                _DOCKER_BINARY,
                "--context",
                _DOCKER_CONTEXT,
                "system",
                "df",
                "--format",
                "json",
            ],
        )
        self.assertTrue(calls[3][1]["capture_output"])
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
        self.assertFalse(by_identifier["docker:build-cache"].preselected)
        self.assertTrue(
            by_identifier["docker:build-cache"].requires_explicit_selection
        )
        self.assertTrue(by_identifier["docker:images"].requires_explicit_selection)
        self.assertTrue(
            by_identifier["docker:containers"].requires_explicit_selection
        )
        self.assertEqual(select_cleanup_items(result.items), [])
        self.assertEqual(
            select_cleanup_items(
                result.items,
                selectors=["docker:build-cache"],
            ),
            [by_identifier["docker:build-cache"]],
        )
        self.assertTrue(by_identifier["docker:build-cache"].actionable)

    def test_scan_binds_context_endpoint_and_daemon_to_every_item(self) -> None:
        binary = _DOCKER_BINARY
        context = "desktop-linux"
        endpoint = "unix:///Users/example/.docker/run/docker.sock"
        daemon_id = "DAEMON:A"
        stdout = _docker_row("Build Cache", "1", "0", "1MB", "500kB")
        calls: list[list[str]] = []

        def runner(command, **_):
            calls.append(command)
            if command == [binary, "context", "show"]:
                return subprocess.CompletedProcess(command, 0, f"{context}\n", "")
            if command == [binary, "context", "inspect", context]:
                inspected = [{
                    "Name": context,
                    "Endpoints": {
                        "docker": {
                            "Host": endpoint,
                            "SkipTLSVerify": False,
                        }
                    },
                }]
                return subprocess.CompletedProcess(
                    command, 0, json.dumps(inspected), ""
                )
            if command == [
                binary,
                "--context",
                context,
                "info",
                "--format",
                "{{json .ID}}",
            ]:
                return subprocess.CompletedProcess(
                    command, 0, json.dumps(daemon_id), ""
                )
            if command == [
                binary,
                "--context",
                context,
                "system",
                "df",
                "--format",
                "json",
            ]:
                return subprocess.CompletedProcess(command, 0, stdout, "")
            raise AssertionError(f"unexpected command: {command}")

        result = scan_docker_resources(
            finder=lambda _: binary,
            runner=runner,
        )

        self.assertTrue(result.complete)
        self.assertEqual(len(result.items), 1)
        binding = json.loads(result.items[0].resource_binding)
        self.assertEqual(binding["context_name"], context)
        self.assertEqual(binding["cli_path"], binary)
        self.assertEqual(binding["endpoint_host"], endpoint)
        self.assertEqual(binding["daemon_id"], daemon_id)
        self.assertEqual(binding["target"], {"kind": "context", "value": context})
        self.assertEqual(
            calls,
            [
                [binary, "context", "show"],
                [binary, "context", "inspect", context],
                [
                    binary,
                    "--context",
                    context,
                    "info",
                    "--format",
                    "{{json .ID}}",
                ],
                [
                    binary,
                    "--context",
                    context,
                    "system",
                    "df",
                    "--format",
                    "json",
                ],
                [binary, "context", "inspect", context],
                [
                    binary,
                    "--context",
                    context,
                    "info",
                    "--format",
                    "{{json .ID}}",
                ],
            ],
        )

    def test_default_context_is_pinned_by_effective_host(self) -> None:
        binary = _DOCKER_BINARY
        endpoint = "tcp://127.0.0.1:2376"
        daemon_id = "DAEMON:DEFAULT"
        stdout = _docker_row("Images", "1", "0", "2MB", "1MB")
        calls: list[list[str]] = []

        def runner(command, **_):
            calls.append(command)
            if command == [binary, "context", "show"]:
                return subprocess.CompletedProcess(command, 0, "default\n", "")
            if command == [binary, "context", "inspect", "default"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps([{
                        "Name": "default",
                        "Endpoints": {
                            "docker": {
                                "Host": endpoint,
                                "SkipTLSVerify": False,
                            }
                        },
                    }]),
                    "",
                )
            if command == [
                binary,
                "--host",
                endpoint,
                "info",
                "--format",
                "{{json .ID}}",
            ]:
                return subprocess.CompletedProcess(
                    command, 0, json.dumps(daemon_id), ""
                )
            if command == [
                binary,
                "--host",
                endpoint,
                "system",
                "df",
                "--format",
                "json",
            ]:
                return subprocess.CompletedProcess(command, 0, stdout, "")
            raise AssertionError(f"unexpected command: {command}")

        result = scan_docker_resources(
            finder=lambda _: binary,
            runner=runner,
        )

        binding = json.loads(result.items[0].resource_binding)
        self.assertTrue(result.complete)
        self.assertEqual(
            binding["target"], {"kind": "host", "value": endpoint}
        )
        self.assertIn(
            [binary, "--host", endpoint, "system", "df", "--format", "json"],
            calls,
        )

    def test_scan_target_change_disables_all_docker_candidates(self) -> None:
        binary = _DOCKER_BINARY
        stdout = _docker_row("Build Cache", "1", "0", "1MB", "500kB")
        info_calls = 0

        def runner(command, **_):
            nonlocal info_calls
            if command == [binary, "context", "show"]:
                return subprocess.CompletedProcess(
                    command, 0, f"{_DOCKER_CONTEXT}\n", ""
                )
            if command == [
                binary,
                "context",
                "inspect",
                _DOCKER_CONTEXT,
            ]:
                return _target_aware_runner()(command)
            if command == [
                binary,
                "--context",
                _DOCKER_CONTEXT,
                "info",
                "--format",
                "{{json .ID}}",
            ]:
                info_calls += 1
                daemon_id = "DAEMON:A" if info_calls == 1 else "DAEMON:B"
                return subprocess.CompletedProcess(
                    command, 0, json.dumps(daemon_id), ""
                )
            if command == [
                binary,
                "--context",
                _DOCKER_CONTEXT,
                "system",
                "df",
                "--format",
                "json",
            ]:
                return subprocess.CompletedProcess(command, 0, stdout, "")
            raise AssertionError(f"unexpected command: {command}")

        result = scan_docker_resources(
            finder=lambda _: binary,
            runner=runner,
        )

        self.assertFalse(result.complete)
        self.assertEqual(result.issues[-1].code, "docker_binding_changed")
        self.assertFalse(result.items[0].actionable)
        self.assertEqual(result.items[0].resource_binding, "")
        self.assertIn("重新扫描", result.items[0].action_block_reason)

    def test_unverified_target_keeps_capacity_read_only(self) -> None:
        binary = _DOCKER_BINARY
        stdout = _docker_row("Build Cache", "1", "0", "1MB", "500kB")

        def runner(command, **_):
            if command == [binary, "context", "show"]:
                return subprocess.CompletedProcess(command, 1, "", "unsupported")
            if command == [binary, "system", "df", "--format", "json"]:
                return subprocess.CompletedProcess(command, 0, stdout, "")
            raise AssertionError(f"unexpected command: {command}")

        result = scan_docker_resources(
            finder=lambda _: binary,
            runner=runner,
        )

        self.assertFalse(result.complete)
        self.assertEqual(result.total, 500_000)
        self.assertFalse(result.items[0].actionable)
        self.assertEqual(result.issues[-1].code, "docker_target_unverified")

    def test_prune_refuses_changed_daemon_before_starting_destructive_command(
        self,
    ) -> None:
        binary = _DOCKER_BINARY
        context = "desktop-linux"
        endpoint = "unix:///Users/example/.docker/run/docker.sock"
        binding = json.dumps(
            {
                "v": 2,
                "kind": "docker",
                "cli_path": binary,
                "context_name": context,
                "target": {"kind": "context", "value": context},
                "endpoint_host": endpoint,
                "skip_tls_verify": False,
                "daemon_id": "DAEMON:A",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        calls: list[list[str]] = []

        def runner(command, **_):
            calls.append(command)
            if command == [binary, "context", "inspect", context]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps([{
                        "Name": context,
                        "Endpoints": {
                            "docker": {
                                "Host": endpoint,
                                "SkipTLSVerify": False,
                            }
                        },
                    }]),
                    "",
                )
            if command == [
                binary,
                "--context",
                context,
                "info",
                "--format",
                "{{json .ID}}",
            ]:
                return subprocess.CompletedProcess(
                    command, 0, json.dumps("DAEMON:B"), ""
                )
            raise AssertionError(f"destructive command started: {command}")

        with self.assertRaisesRegex(
            DockerPruneError, "Docker target.*变化|重新扫描"
        ) as raised:
            prune_docker_resource(
                "docker:build-cache",
                resource_binding=binding,
                finder=lambda _: binary,
                runner=runner,
            )

        self.assertFalse(raised.exception.side_effect_unknown)
        self.assertFalse(any("prune" in command for command in calls))

    def test_prune_refuses_changed_endpoint_or_tls_before_prune(self) -> None:
        for changed_endpoint, changed_tls in (
            ("ssh://private.example.test", False),
            (_DOCKER_ENDPOINT, True),
        ):
            with self.subTest(
                endpoint=changed_endpoint,
                skip_tls_verify=changed_tls,
            ):
                calls: list[list[str]] = []

                def runner(
                    command,
                    *,
                    _calls=calls,
                    _endpoint=changed_endpoint,
                    _tls=changed_tls,
                    **_,
                ):
                    _calls.append(command)
                    if command == [
                        _DOCKER_BINARY,
                        "context",
                        "inspect",
                        _DOCKER_CONTEXT,
                    ]:
                        return subprocess.CompletedProcess(
                            command,
                            0,
                            json.dumps([{
                                "Name": _DOCKER_CONTEXT,
                                "Endpoints": {
                                    "docker": {
                                        "Host": _endpoint,
                                        "SkipTLSVerify": _tls,
                                    }
                                },
                            }]),
                            "",
                        )
                    if command == [
                        _DOCKER_BINARY,
                        "--context",
                        _DOCKER_CONTEXT,
                        "info",
                        "--format",
                        "{{json .ID}}",
                    ]:
                        return subprocess.CompletedProcess(
                            command, 0, json.dumps(_DOCKER_DAEMON_ID), ""
                        )
                    raise AssertionError(
                        f"destructive command started: {command}"
                    )

                with self.assertRaisesRegex(
                    DockerPruneError, "Docker target.*变化|重新扫描"
                ) as raised:
                    prune_docker_resource(
                        "docker:build-cache",
                        resource_binding=_docker_binding(),
                        finder=lambda _: _DOCKER_BINARY,
                        runner=runner,
                    )

                self.assertFalse(raised.exception.side_effect_unknown)
                self.assertFalse(any("prune" in command for command in calls))

    def test_malformed_binding_fails_before_any_docker_command(self) -> None:
        base = json.loads(_docker_binding())
        malformed: dict[str, str] = {}

        payload = dict(base)
        payload["unexpected"] = True
        malformed["extra top-level field"] = json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        )

        payload = dict(base)
        payload["v"] = True
        malformed["boolean version"] = json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        )

        payload = dict(base)
        payload["v"] = 1
        malformed["legacy version"] = json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        )

        payload = dict(base)
        del payload["cli_path"]
        malformed["missing CLI path"] = json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        )

        payload = dict(base)
        payload["cli_path"] = "relative/docker"
        malformed["relative CLI path"] = json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        )

        payload = dict(base)
        payload["skip_tls_verify"] = 0
        malformed["non-boolean TLS mode"] = json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        )

        payload = dict(base)
        payload["target"] = {"kind": [], "value": _DOCKER_CONTEXT}
        malformed["unhashable target kind"] = json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        )

        payload = dict(base)
        payload["daemon_id"] = ""
        malformed["empty daemon ID"] = json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        )

        malformed["non-canonical whitespace"] = json.dumps(base)
        malformed["duplicate key"] = _docker_binding().replace(
            '"v":2', '"v":2,"v":2', 1
        )
        runner = mock.Mock(side_effect=AssertionError("不应启动 Docker CLI"))

        for case, binding in malformed.items():
            with self.subTest(case=case), self.assertRaisesRegex(
                DockerPruneError, "binding"
            ) as raised:
                prune_docker_resource(
                    "docker:build-cache",
                    resource_binding=binding,
                    finder=lambda _: _DOCKER_BINARY,
                    runner=runner,
                )

            self.assertFalse(raised.exception.side_effect_unknown)
        runner.assert_not_called()

    def test_prune_uses_bound_effective_host_for_default_context(self) -> None:
        endpoint = "tcp://127.0.0.1:2376"
        daemon_id = "DAEMON:DEFAULT"
        binding = json.dumps(
            {
                "v": 2,
                "kind": "docker",
                "cli_path": _DOCKER_BINARY,
                "context_name": "default",
                "target": {"kind": "host", "value": endpoint},
                "endpoint_host": endpoint,
                "skip_tls_verify": False,
                "daemon_id": daemon_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        calls: list[list[str]] = []

        def runner(command, **_):
            calls.append(command)
            if command == [_DOCKER_BINARY, "context", "inspect", "default"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps([{
                        "Name": "default",
                        "Endpoints": {
                            "docker": {
                                "Host": endpoint,
                                "SkipTLSVerify": False,
                            }
                        },
                    }]),
                    "",
                )
            if command == [
                _DOCKER_BINARY,
                "--host",
                endpoint,
                "info",
                "--format",
                "{{json .ID}}",
            ]:
                return subprocess.CompletedProcess(
                    command, 0, json.dumps(daemon_id), ""
                )
            if command == [
                _DOCKER_BINARY,
                "--host",
                endpoint,
                "builder",
                "prune",
                "--all",
                "--force",
            ]:
                return subprocess.CompletedProcess(
                    command, 0, "Total reclaimed space: 1MB\n", ""
                )
            raise AssertionError(f"unexpected command: {command}")

        result = prune_docker_resource(
            "docker:build-cache",
            resource_binding=binding,
            finder=lambda _: _DOCKER_BINARY,
            runner=runner,
        )

        self.assertEqual(result.reclaimed_bytes, 1_000_000)
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[-1][1:3], ["--host", endpoint])

    def test_prune_refuses_cli_path_switch_before_any_docker_command(self) -> None:
        switched_binary = os.path.realpath("/tmp/openclean-other-docker")
        runner = mock.Mock(side_effect=AssertionError("不应启动 Docker CLI"))

        with self.assertRaisesRegex(
            DockerPruneError, "Docker CLI.*变化|重新扫描"
        ) as raised:
            prune_docker_resource(
                "docker:build-cache",
                resource_binding=_docker_binding(),
                finder=lambda _: switched_binary,
                runner=runner,
            )

        self.assertFalse(raised.exception.side_effect_unknown)
        runner.assert_not_called()

    def test_scan_and_prune_bind_canonical_cli_symlink_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "Docker CLI"
            target.write_text("synthetic", encoding="utf-8")
            symlink = root / "docker"
            symlink.symlink_to(target)
            canonical = str(target.resolve())
            stdout = _docker_row(
                "Build Cache", "1", "0", "1MB", "500kB (50%)"
            )
            calls: list[list[str]] = []

            def runner(command, **_):
                calls.append(command)
                if command == [canonical, "context", "show"]:
                    return subprocess.CompletedProcess(
                        command, 0, f"{_DOCKER_CONTEXT}\n", ""
                    )
                if command == [
                    canonical,
                    "context",
                    "inspect",
                    _DOCKER_CONTEXT,
                ]:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        json.dumps([{
                            "Name": _DOCKER_CONTEXT,
                            "Endpoints": {
                                "docker": {
                                    "Host": _DOCKER_ENDPOINT,
                                    "SkipTLSVerify": False,
                                }
                            },
                        }]),
                        "",
                    )
                if command == [
                    canonical,
                    "--context",
                    _DOCKER_CONTEXT,
                    "info",
                    "--format",
                    "{{json .ID}}",
                ]:
                    return subprocess.CompletedProcess(
                        command, 0, json.dumps(_DOCKER_DAEMON_ID), ""
                    )
                if command == [
                    canonical,
                    "--context",
                    _DOCKER_CONTEXT,
                    "system",
                    "df",
                    "--format",
                    "json",
                ]:
                    return subprocess.CompletedProcess(command, 0, stdout, "")
                if command == [
                    canonical,
                    "--context",
                    _DOCKER_CONTEXT,
                    "builder",
                    "prune",
                    "--all",
                    "--force",
                ]:
                    return subprocess.CompletedProcess(
                        command, 0, "Total reclaimed space: 500kB\n", ""
                    )
                raise AssertionError(f"unexpected command: {command}")

            scan = scan_docker_resources(
                finder=lambda _: str(symlink),
                runner=runner,
            )
            binding = json.loads(scan.items[0].resource_binding)
            pruned = prune_docker_resource(
                "docker:build-cache",
                resource_binding=scan.items[0].resource_binding,
                finder=lambda _: str(symlink),
                runner=runner,
            )

            self.assertTrue(scan.complete)
            self.assertEqual(binding["v"], 2)
            self.assertEqual(binding["cli_path"], canonical)
            self.assertEqual(pruned.reclaimed_bytes, 500_000)
            self.assertTrue(all(command[0] == canonical for command in calls))

    def test_prune_maps_only_audited_resource_identifiers(self) -> None:
        expected = {
            "docker:build-cache": [
                _DOCKER_BINARY,
                "--context",
                _DOCKER_CONTEXT,
                "builder",
                "prune",
                "--all",
                "--force",
            ],
            "docker:images": [
                _DOCKER_BINARY,
                "--context",
                _DOCKER_CONTEXT,
                "image",
                "prune",
                "--all",
                "--force",
            ],
            "docker:containers": [
                _DOCKER_BINARY,
                "--context",
                _DOCKER_CONTEXT,
                "container",
                "prune",
                "--force",
            ],
        }
        for identifier, expected_command in expected.items():
            with self.subTest(identifier=identifier):
                calls: list[list[str]] = []

                def prune(command, **_):
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        "Total reclaimed space: 1.5 MB\n",
                        "",
                    )

                result = prune_docker_resource(
                    identifier,
                    resource_binding=_docker_binding(),
                    finder=lambda _: _DOCKER_BINARY,
                    runner=_target_aware_runner(
                        prune=prune,
                        calls=calls,
                    ),
                )

                self.assertEqual(calls[-1], expected_command)
                self.assertEqual(len(calls), 3)
                self.assertEqual(result.reclaimed_bytes, 1_500_000)
                self.assertIn("prune 已完成", result.message)

        with self.assertRaisesRegex(DockerPruneError, "不支持自动清理"):
            prune_docker_resource("docker:local-volumes")
        with self.assertRaisesRegex(DockerPruneError, "不支持自动清理"):
            prune_docker_resource("docker:unknown")

    def test_prune_failure_modes_are_explicit_and_never_guess_reclaimed_size(self) -> None:
        with self.assertRaisesRegex(
            DockerPruneError, "未找到 Docker CLI"
        ) as missing:
            prune_docker_resource(
                "docker:build-cache",
                resource_binding=_docker_binding(),
                finder=lambda _: None,
            )
        self.assertFalse(missing.exception.side_effect_unknown)

        private_error = (
            f"daemon unavailable at {_DOCKER_ENDPOINT}; "
            f"identity={_DOCKER_DAEMON_ID}"
        )
        with self.assertRaisesRegex(
            DockerPruneError, "Docker prune 失败，退出码 1"
        ) as nonzero:
            def nonzero_prune(command, **_):
                return subprocess.CompletedProcess(
                    command, 1, "", private_error
                )

            prune_docker_resource(
                "docker:build-cache",
                resource_binding=_docker_binding(),
                finder=lambda _: _DOCKER_BINARY,
                runner=_target_aware_runner(
                    prune=nonzero_prune,
                ),
            )
        self.assertTrue(nonzero.exception.side_effect_unknown)
        self.assertNotIn(_DOCKER_ENDPOINT, str(nonzero.exception))
        self.assertNotIn(_DOCKER_DAEMON_ID, str(nonzero.exception))

        def timeout(command, **_):
            raise subprocess.TimeoutExpired(command, 0.01)

        with self.assertRaisesRegex(DockerPruneError, "0.01 秒") as timed_out:
            prune_docker_resource(
                "docker:build-cache",
                resource_binding=_docker_binding(),
                finder=lambda _: _DOCKER_BINARY,
                runner=_target_aware_runner(prune=timeout),
                timeout=0.01,
            )
        self.assertTrue(timed_out.exception.side_effect_unknown)

        def no_size(command, **_):
            return subprocess.CompletedProcess(
                command, 0, "prune complete without size", ""
            )

        result = prune_docker_resource(
            "docker:build-cache",
            resource_binding=_docker_binding(),
            finder=lambda _: _DOCKER_BINARY,
            runner=_target_aware_runner(prune=no_size),
        )
        self.assertEqual(result.reclaimed_bytes, 0)
        self.assertIn("未提供可解析", result.message)

        def malformed_size(command, **_):
            return subprocess.CompletedProcess(
                command,
                0,
                "Total reclaimed space: definitely-not-a-size",
                "",
            )

        malformed = prune_docker_resource(
            "docker:build-cache",
            resource_binding=_docker_binding(),
            finder=lambda _: _DOCKER_BINARY,
            runner=_target_aware_runner(prune=malformed_size),
        )
        self.assertEqual(malformed.reclaimed_bytes, 0)
        self.assertIn("已完成", malformed.message)
        self.assertIn("无法解析", malformed.message)

    def test_zero_reclaimable_rows_are_not_candidates(self) -> None:
        stdout = _docker_row("Images", "1", "1", "20MB", "0B (0%)")

        result = scan_docker_resources(
            finder=lambda _: _DOCKER_BINARY,
            runner=_target_aware_runner(df_stdout=stdout),
        )

        self.assertTrue(result.complete)
        self.assertEqual(result.items, [])

    def test_daemon_failure_is_a_structured_partial_result(self) -> None:
        def runner(command, **_):
            return subprocess.CompletedProcess(
                command,
                1,
                "",
                f"Cannot connect to {_DOCKER_ENDPOINT}",
            )

        result = scan_docker_resources(
            finder=lambda _: _DOCKER_BINARY,
            runner=runner,
        )

        self.assertFalse(result.complete)
        self.assertEqual(result.items, [])
        self.assertEqual(result.issues[0].code, "tool_unavailable")
        self.assertEqual(result.issues[0].task, "docker-system-df")
        self.assertEqual(
            result.issues[0].message,
            "Docker CLI 执行失败，退出码 1",
        )
        self.assertNotIn(_DOCKER_ENDPOINT, result.issues[0].message)

    def test_timeout_is_a_structured_partial_result(self) -> None:
        def runner(command, **_):
            raise subprocess.TimeoutExpired(command, 0.01)

        result = scan_docker_resources(
            finder=lambda _: _DOCKER_BINARY,
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
            finder=lambda _: _DOCKER_BINARY,
            runner=_target_aware_runner(df_stdout=stdout),
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
                        resource_binding=_docker_binding(),
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
            serialized = json.dumps(payload, ensure_ascii=False)
            self.assertNotIn("resource_binding", serialized)
            self.assertNotIn(_DOCKER_BINARY, serialized)
            self.assertNotIn(_DOCKER_ENDPOINT, serialized)
            self.assertNotIn(_DOCKER_DAEMON_ID, serialized)
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
