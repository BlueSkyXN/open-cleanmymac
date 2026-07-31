from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from openclean.cleanup import CleanupOutcome, CleanupReport
from openclean.cli import _print_clean_report, main
from openclean.docker import (
    DockerPruneError,
    DockerPruneResult,
    DockerTargetIdentity,
    encode_docker_resource_binding,
)
from openclean.macos import TrashDiscovery
from openclean.models import Item, ScanResult
from openclean.scanpoints import DOMAINS, ScanPoint
from openclean.tui import ReviewResult, TUIUnavailable

DOCKER_BINDING = encode_docker_resource_binding(
    DockerTargetIdentity(
        context_name="desktop-linux",
        target_kind="context",
        target_value="desktop-linux",
        endpoint_host="unix:///Users/example/.docker/run/docker.sock",
        skip_tls_verify=False,
        daemon_id="DAEMON:A",
    )
)


class _TTYBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


def _rules(path: Path) -> Path:
    rules = path / "rules.json"
    rules.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    return rules


class CleanupCliTests(unittest.TestCase):
    def test_text_report_distinguishes_preview_from_execution(self) -> None:
        item = Item(
            path=Path("/Preview/Library/Caches/pip"),
            size=4096,
            category="安全缓存",
            safety="safe",
            domain="developer",
            preselected=True,
        )
        result = ScanResult(items=[item])

        preview = io.StringIO()
        with contextlib.redirect_stdout(preview):
            _print_clean_report(result, False, "dev")

        self.assertIn("清理扫描结果（只读预览，不会删除）", preview.getvalue())
        self.assertIn("当前选择", preview.getvalue())

        report = CleanupReport(
            outcomes=[
                CleanupOutcome(
                    item=item,
                    status="moved_to_trash",
                    bytes_affected=item.size,
                    destination=Path("/Preview/.Trash/pip"),
                )
            ]
        )
        execution = io.StringIO()
        with contextlib.redirect_stdout(execution):
            _print_clean_report(result, False, "dev", report)

        output = execution.getvalue()
        self.assertIn("清理执行结果", output)
        self.assertIn("当前选择", output)
        self.assertNotIn("只读预览", output)
        self.assertNotIn("默认预选", output)

    def test_preview_never_mutates_even_with_selection_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            cache = home / "cache"
            cache.mkdir(parents=True)
            (cache / "data.bin").write_bytes(b"data")
            rules = _rules(home)
            stdout = io.StringIO()
            points = [ScanPoint("确认缓存", (str(cache),), "confirm")]

            with mock.patch.dict(os.environ, {"HOME": str(home)}), mock.patch.dict(
                DOMAINS, {"developer": points}
            ), contextlib.redirect_stdout(stdout):
                status = main(
                    [
                        "clean",
                        "dev",
                        "--include-confirm",
                        "--rules",
                        str(rules),
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(status, 0)
            self.assertEqual(payload["mode"], "preview")
            self.assertIsNone(payload["cleanup"])
            self.assertTrue(payload["categories"][0]["items"][0]["preselected"])
            self.assertTrue(cache.exists())
            self.assertFalse((home / ".Trash").exists())

    def test_yes_moves_default_safe_item_and_reports_staging_not_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            cache = home / "cache"
            cache.mkdir(parents=True)
            (cache / "data.bin").write_bytes(b"data")
            rules = _rules(home)
            stdout = io.StringIO()

            with mock.patch.dict(os.environ, {"HOME": str(home)}), mock.patch.dict(
                DOMAINS,
                {"developer": [ScanPoint("安全缓存", (str(cache),))]},
            ), contextlib.redirect_stdout(stdout):
                status = main(
                    [
                        "clean",
                        "dev",
                        "--yes",
                        "--rules",
                        str(rules),
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            cleanup = payload["cleanup"]
            self.assertEqual(status, 0)
            self.assertEqual(payload["mode"], "result")
            self.assertTrue(cleanup["complete"])
            self.assertEqual(cleanup["selected_count"], 1)
            self.assertGreater(cleanup["moved_to_trash_bytes"], 0)
            self.assertEqual(cleanup["permanently_deleted_bytes"], 0)
            self.assertEqual(
                cleanup["outcomes"][0]["status"], "moved_to_trash"
            )
            self.assertFalse(cache.exists())
            self.assertTrue((home / ".Trash" / "cache" / "data.bin").exists())

    def test_confirm_and_critical_need_separate_explicit_gates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            confirm = home / "confirm"
            critical = home / "critical"
            confirm.mkdir(parents=True)
            critical.mkdir()
            (confirm / "data.bin").write_bytes(b"confirm")
            (critical / "data.bin").write_bytes(b"critical")
            rules = _rules(home)
            points = [
                ScanPoint("确认项", (str(confirm),), "confirm"),
                ScanPoint("关键项", (str(critical),), "critical"),
            ]
            stderr = io.StringIO()

            with mock.patch.dict(os.environ, {"HOME": str(home)}), mock.patch.dict(
                DOMAINS, {"developer": points}
            ), contextlib.redirect_stderr(stderr):
                rejected = main(
                    [
                        "clean",
                        "dev",
                        "--yes",
                        "--select",
                        str(confirm),
                        "--rules",
                        str(rules),
                    ]
                )

            self.assertEqual(rejected, 2)
            self.assertIn("--include-confirm", stderr.getvalue())
            self.assertTrue(confirm.exists())
            self.assertTrue(critical.exists())

            stdout = io.StringIO()
            with mock.patch.dict(os.environ, {"HOME": str(home)}), mock.patch.dict(
                DOMAINS, {"developer": points}
            ), contextlib.redirect_stdout(stdout):
                status = main(
                    [
                        "clean",
                        "dev",
                        "--yes",
                        "--include-confirm",
                        "--include-critical",
                        "--rules",
                        str(rules),
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(status, 0)
            self.assertEqual(payload["cleanup"]["selected_count"], 2)
            self.assertFalse(confirm.exists())
            self.assertFalse(critical.exists())

    def test_force_requires_yes_and_rejects_selection_expansion_before_scan(self) -> None:
        stderr = io.StringIO()
        with mock.patch("openclean.cli.scan_domains") as scanner, \
                contextlib.redirect_stderr(stderr):
            missing_yes = main(["clean", "dev", "--force"])
            conflicting = main(
                ["clean", "dev", "--force", "--yes", "--all"]
            )

        self.assertEqual(missing_yes, 2)
        self.assertEqual(conflicting, 2)
        self.assertIn("--yes", stderr.getvalue())
        scanner.assert_not_called()

    def test_select_and_all_are_rejected_before_scan(self) -> None:
        stderr = io.StringIO()
        with mock.patch("openclean.cli.scan_domains") as scanner, \
                contextlib.redirect_stderr(stderr):
            status = main(
                [
                    "clean",
                    "dev",
                    "--select",
                    "/tmp/exact",
                    "--all",
                ]
            )

        self.assertEqual(status, 2)
        self.assertIn("精确选择模式", stderr.getvalue())
        scanner.assert_not_called()

    def test_exact_select_does_not_inherit_default_or_tier_selections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rules = _rules(root)
            safe = Item(
                path=root / "safe",
                size=100,
                category="安全缓存",
                safety="safe",
                domain="developer",
                preselected=True,
            )
            other_confirm = Item(
                path=root / "other-confirm",
                size=200,
                category="其他确认项",
                safety="confirm",
                domain="developer",
            )
            exact = Item(
                path=root / "exact",
                size=300,
                category="精确确认项",
                safety="confirm",
                domain="developer",
                requires_explicit_selection=True,
            )
            stdout = io.StringIO()

            with mock.patch(
                "openclean.cli.scan_domains",
                return_value=ScanResult(items=[safe, other_confirm, exact]),
            ), contextlib.redirect_stdout(stdout):
                status = main(
                    [
                        "clean",
                        "dev",
                        "--select",
                        str(exact.path),
                        "--include-confirm",
                        "--rules",
                        str(rules),
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            selected = {
                item["path"]
                for category in payload["categories"]
                for item in category["items"]
                if item["preselected"]
            }
            self.assertEqual(status, 0)
            self.assertEqual(payload["mode"], "preview")
            self.assertIsNone(payload["cleanup"])
            self.assertEqual(selected, {str(exact.path)})

    def test_force_with_yes_executes_only_default_preselection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            safe = home / "safe"
            confirm = home / "confirm"
            safe.mkdir(parents=True)
            confirm.mkdir()
            (safe / "safe.bin").write_bytes(b"safe")
            (confirm / "confirm.bin").write_bytes(b"confirm")
            rules = _rules(home)
            stdout = io.StringIO()
            points = [
                ScanPoint("安全项", (str(safe),)),
                ScanPoint("确认项", (str(confirm),), "confirm"),
            ]

            with mock.patch.dict(os.environ, {"HOME": str(home)}), mock.patch.dict(
                DOMAINS, {"developer": points}
            ), contextlib.redirect_stdout(stdout):
                status = main(
                    [
                        "clean",
                        "dev",
                        "--force",
                        "--yes",
                        "--rules",
                        str(rules),
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(status, 0)
            self.assertEqual(payload["cleanup"]["selected_count"], 1)
            self.assertFalse(safe.exists())
            self.assertTrue(confirm.exists())

    def test_purge_yes_moves_only_preselected_old_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = home / "project"
            artifact = project / "node_modules"
            artifact.mkdir(parents=True)
            payload_file = artifact / "package.bin"
            payload_file.write_bytes(b"package")
            old = time.time() - 8 * 24 * 60 * 60
            os.utime(payload_file, (old, old))
            os.utime(artifact, (old, old))
            rules = _rules(home)
            stdout = io.StringIO()

            with mock.patch.dict(os.environ, {"HOME": str(home)}), \
                    contextlib.redirect_stdout(stdout):
                status = main(
                    [
                        "purge",
                        str(project),
                        "--yes",
                        "--rules",
                        str(rules),
                        "--json",
                    ]
                )

            result = json.loads(stdout.getvalue())
            self.assertEqual(status, 0)
            self.assertEqual(result["mode"], "result")
            self.assertEqual(result["cleanup"]["selected_count"], 1)
            self.assertTrue(project.exists())
            self.assertFalse(artifact.exists())
            self.assertTrue(
                (home / ".Trash" / "node_modules" / "package.bin").exists()
            )

    def test_clean_trash_permanently_deletes_contents_but_preserves_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            trash = home / ".Trash"
            trash.mkdir(parents=True)
            (trash / "old.bin").write_bytes(b"old")
            rules = _rules(home)
            stdout = io.StringIO()
            discovery = TrashDiscovery(paths=(trash,))

            with mock.patch.dict(os.environ, {"HOME": str(home)}), mock.patch(
                "openclean.engine.discover_trash_paths",
                return_value=discovery,
            ), contextlib.redirect_stdout(stdout):
                status = main(
                    [
                        "clean",
                        "trash",
                        "--yes",
                        "--include-confirm",
                        "--rules",
                        str(rules),
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(status, 0)
            self.assertEqual(
                payload["cleanup"]["outcomes"][0]["status"], "deleted"
            )
            self.assertGreater(
                payload["cleanup"]["permanently_deleted_bytes"], 0
            )
            self.assertTrue(trash.is_dir())
            self.assertEqual(list(trash.iterdir()), [])

    def test_clean_dev_executes_preselected_docker_build_cache_via_prune(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            rules = _rules(home)
            docker_item = Item(
                path=None,
                size=2_000_000,
                category="Docker 构建缓存",
                preselected=True,
                domain="developer",
                resource_kind="docker",
                identifier="docker:build-cache",
                resource_binding=DOCKER_BINDING,
            )
            stdout = io.StringIO()

            with mock.patch.dict(os.environ, {"HOME": str(home)}), mock.patch.dict(
                DOMAINS,
                {
                    "developer": [
                        ScanPoint("Docker 资源", (), scanner="docker")
                    ]
                },
            ), mock.patch(
                "openclean.engine.scan_docker_resources",
                return_value=ScanResult(items=[docker_item]),
            ), mock.patch(
                "openclean.cleanup.prune_docker_resource",
                return_value=DockerPruneResult(1_500_000, "pruned"),
            ) as prune, contextlib.redirect_stdout(stdout):
                status = main(
                    [
                        "clean",
                        "dev",
                        "--yes",
                        "--rules",
                        str(rules),
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(status, 0)
            self.assertEqual(
                payload["cleanup"]["outcomes"][0]["status"], "pruned"
            )
            self.assertEqual(
                payload["cleanup"]["permanently_deleted_bytes"],
                1_500_000,
            )
            prune.assert_called_once()

    def test_clean_dev_reports_started_docker_prune_as_partial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            rules = _rules(home)
            docker_item = Item(
                path=None,
                size=2_000_000,
                category="Docker 构建缓存",
                preselected=False,
                domain="developer",
                requires_explicit_selection=True,
                resource_kind="docker",
                identifier="docker:build-cache",
                resource_binding=DOCKER_BINDING,
            )
            stdout = io.StringIO()

            with mock.patch.dict(os.environ, {"HOME": str(home)}), mock.patch.dict(
                DOMAINS,
                {
                    "developer": [
                        ScanPoint("Docker 资源", (), scanner="docker")
                    ]
                },
            ), mock.patch(
                "openclean.engine.scan_docker_resources",
                return_value=ScanResult(items=[docker_item]),
            ), mock.patch(
                "openclean.cleanup.prune_docker_resource",
                side_effect=DockerPruneError(
                    "Docker prune timed out",
                    side_effect_unknown=True,
                ),
            ), contextlib.redirect_stdout(stdout):
                status = main(
                    [
                        "clean",
                        "dev",
                        "--select",
                        "docker:build-cache",
                        "--yes",
                        "--rules",
                        str(rules),
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            cleanup = payload["cleanup"]
            self.assertEqual(status, 1)
            self.assertFalse(cleanup["complete"])
            self.assertEqual(cleanup["outcomes"][0]["status"], "partial")
            self.assertEqual(cleanup["permanently_deleted_bytes"], 0)
            self.assertIn("可能已经发生", cleanup["outcomes"][0]["message"])

    def test_tty_review_changes_preview_selection_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            cache = home / "cache"
            cache.mkdir(parents=True)
            (cache / "data.bin").write_bytes(b"data")
            rules = _rules(home)
            stdin = _TTYBuffer()
            stdout = _TTYBuffer()

            with mock.patch.dict(os.environ, {"HOME": str(home)}), mock.patch.dict(
                DOMAINS,
                {"developer": [ScanPoint("安全缓存", (str(cache),))]},
            ), mock.patch.object(sys, "stdin", stdin), contextlib.redirect_stdout(
                stdout
            ), mock.patch(
                "openclean.cli.review_cleanup",
                return_value=ReviewResult((), True, False, False),
            ) as review:
                status = main(
                    ["clean", "dev", "--rules", str(rules)]
                )

            self.assertEqual(status, 0)
            review.assert_called_once()
            self.assertTrue(cache.exists())
            self.assertFalse((home / ".Trash").exists())
            self.assertIn("当前选择 0B", stdout.getvalue())

    def test_tty_yes_executes_only_after_review_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            cache = home / "cache"
            cache.mkdir(parents=True)
            (cache / "data.bin").write_bytes(b"data")
            rules = _rules(home)
            stdin = _TTYBuffer()
            stdout = _TTYBuffer()

            def approve(groups, **_):
                return ReviewResult(
                    (groups[0].items[0],), True, True, False
                )

            with mock.patch.dict(os.environ, {"HOME": str(home)}), mock.patch.dict(
                DOMAINS,
                {"developer": [ScanPoint("安全缓存", (str(cache),))]},
            ), mock.patch.object(sys, "stdin", stdin), contextlib.redirect_stdout(
                stdout
            ), mock.patch(
                "openclean.cli.review_cleanup", side_effect=approve
            ) as review:
                status = main(
                    ["clean", "dev", "--yes", "--rules", str(rules)]
                )

            self.assertEqual(status, 0)
            review.assert_called_once()
            self.assertFalse(cache.exists())
            self.assertTrue((home / ".Trash" / "cache").exists())
            self.assertIn("moved_to_trash", stdout.getvalue())

    def test_tty_cancel_and_initialization_failure_never_execute(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            cache = home / "cache"
            cache.mkdir(parents=True)
            (cache / "data.bin").write_bytes(b"data")
            rules = _rules(home)
            stdin = _TTYBuffer()

            with mock.patch.dict(os.environ, {"HOME": str(home)}), mock.patch.dict(
                DOMAINS,
                {"developer": [ScanPoint("安全缓存", (str(cache),))]},
            ), mock.patch.object(sys, "stdin", stdin), contextlib.redirect_stdout(
                _TTYBuffer()
            ), mock.patch(
                "openclean.cli.review_cleanup",
                return_value=ReviewResult((), False, False, True),
            ):
                cancelled = main(
                    ["clean", "dev", "--yes", "--rules", str(rules)]
                )

            stderr = io.StringIO()
            with mock.patch.dict(os.environ, {"HOME": str(home)}), mock.patch.dict(
                DOMAINS,
                {"developer": [ScanPoint("安全缓存", (str(cache),))]},
            ), mock.patch.object(sys, "stdin", stdin), contextlib.redirect_stdout(
                _TTYBuffer()
            ), contextlib.redirect_stderr(stderr), mock.patch(
                "openclean.cli.review_cleanup",
                side_effect=TUIUnavailable("terminal failed"),
            ):
                failed = main(
                    ["clean", "dev", "--yes", "--rules", str(rules)]
                )

            self.assertEqual(cancelled, 0)
            self.assertEqual(failed, 2)
            self.assertIn("terminal failed", stderr.getvalue())
            self.assertTrue(cache.exists())
            self.assertFalse((home / ".Trash").exists())

    def test_purge_tty_review_is_grouped_by_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            work = home / "work"
            old = time.time() - 8 * 24 * 60 * 60
            for name in ("alpha", "beta"):
                project = work / name
                artifact = project / "node_modules"
                artifact.mkdir(parents=True)
                (project / "package.json").write_text("{}", encoding="utf-8")
                payload = artifact / "data.bin"
                payload.write_bytes(name.encode())
                os.utime(payload, (old, old))
                os.utime(artifact, (old, old))
            rules = _rules(home)
            stdin = _TTYBuffer()
            captured = []

            def cancel(groups, **_):
                captured.extend(groups)
                return ReviewResult((), False, False, True)

            with mock.patch.dict(os.environ, {"HOME": str(home)}), mock.patch.object(
                sys, "stdin", stdin
            ), contextlib.redirect_stdout(_TTYBuffer()), mock.patch(
                "openclean.cli.review_cleanup", side_effect=cancel
            ):
                status = main(
                    ["purge", str(work), "--rules", str(rules)]
                )

            self.assertEqual(status, 0)
            self.assertEqual({group.label for group in captured}, {"alpha", "beta"})
            self.assertTrue((work / "alpha" / "node_modules").exists())
            self.assertTrue((work / "beta" / "node_modules").exists())


if __name__ == "__main__":
    unittest.main()
