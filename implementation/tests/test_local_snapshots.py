from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openclean.analyzer import analyze_path
from openclean.cli import main
from openclean.macos import (
    LocalSnapshotDiscovery,
    discover_local_snapshots,
)
from openclean.models import ScanIssue


class LocalSnapshotDiscoveryTests(unittest.TestCase):
    def test_parses_time_machine_names_and_deduplicates(self) -> None:
        calls = []
        output = (
            "Snapshots for disk /:\n"
            "com.apple.TimeMachine.2026-07-28-010203.local\n"
            "com.apple.TimeMachine.2026-07-29-040506.local\n"
            "com.apple.TimeMachine.2026-07-28-010203.local\n"
        )

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, output, "")

        discovery = discover_local_snapshots(
            Path("/"), runner=runner, timeout=1.25
        )

        self.assertEqual(
            discovery.snapshots,
            (
                "com.apple.TimeMachine.2026-07-28-010203.local",
                "com.apple.TimeMachine.2026-07-29-040506.local",
            ),
        )
        self.assertEqual(discovery.issues, ())
        self.assertEqual(
            calls[0][0],
            ["/usr/bin/tmutil", "listlocalsnapshots", "/"],
        )
        self.assertEqual(calls[0][1]["timeout"], 1.25)

    def test_header_only_means_successful_empty_snapshot_list(self) -> None:
        discovery = discover_local_snapshots(
            Path("/"),
            runner=lambda command, **_: subprocess.CompletedProcess(
                command,
                0,
                "Snapshots for disk /:\n",
                "",
            ),
        )

        self.assertEqual(discovery.snapshots, ())
        self.assertEqual(discovery.issues, ())

    def test_command_failure_is_nonblocking_information(self) -> None:
        discovery = discover_local_snapshots(
            Path("/"),
            runner=lambda command, **_: subprocess.CompletedProcess(
                command,
                1,
                "",
                "permission denied",
            ),
        )

        self.assertEqual(discovery.snapshots, ())
        self.assertEqual(discovery.issues[0].code, "snapshot_discovery_failed")
        self.assertFalse(discovery.issues[0].blocking)
        self.assertIn("permission denied", discovery.issues[0].message)


class AnalyzeSnapshotIntegrationTests(unittest.TestCase):
    def _discovery(self, root: Path) -> LocalSnapshotDiscovery:
        return LocalSnapshotDiscovery(
            mount_point=root,
            snapshots=("com.apple.TimeMachine.2026-07-29-040506.local",),
        )

    def test_mount_root_analysis_includes_readonly_snapshot_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data.bin").write_bytes(b"data")
            discovery = self._discovery(root)

            with mock.patch(
                "openclean.analyzer.volume_mount_point",
                return_value=root,
            ), mock.patch(
                "openclean.analyzer.discover_local_snapshots",
                return_value=discovery,
            ) as snapshot_scan:
                analysis = analyze_path(root)

            self.assertTrue(analysis.complete)
            self.assertTrue(analysis.local_snapshots_checked)
            self.assertEqual(analysis.snapshot_mount_point, root)
            self.assertEqual(analysis.local_snapshots, discovery.snapshots)
            self.assertIsNone(analysis.local_snapshot_size)
            snapshot_scan.assert_called_once_with(root)

    def test_subdirectory_analysis_does_not_repeat_volume_snapshot_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mount = Path(tmp)
            root = mount / "subdirectory"
            root.mkdir()

            with mock.patch(
                "openclean.analyzer.volume_mount_point",
                return_value=mount,
            ), mock.patch(
                "openclean.analyzer.discover_local_snapshots"
            ) as snapshot_scan:
                analysis = analyze_path(root)

            self.assertFalse(analysis.local_snapshots_checked)
            self.assertEqual(analysis.snapshot_mount_point, mount)
            snapshot_scan.assert_not_called()

    def test_snapshot_failure_does_not_make_space_scan_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            issue = ScanIssue(
                code="snapshot_discovery_failed",
                message="unavailable",
                task="time-machine-local-snapshots",
                path=root,
                blocking=False,
            )
            discovery = LocalSnapshotDiscovery(
                mount_point=root,
                issues=(issue,),
            )

            with mock.patch(
                "openclean.analyzer.volume_mount_point",
                return_value=root,
            ), mock.patch(
                "openclean.analyzer.discover_local_snapshots",
                return_value=discovery,
            ):
                analysis = analyze_path(root)

            self.assertTrue(analysis.complete)
            self.assertFalse(analysis.local_snapshots_checked)
            self.assertEqual(analysis.issues, [issue])

    def test_cli_json_marks_snapshot_size_unknown_and_deletion_unsupported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data.bin").write_bytes(b"data")
            stdout = io.StringIO()

            with mock.patch(
                "openclean.analyzer.volume_mount_point",
                return_value=root,
            ), mock.patch(
                "openclean.analyzer.discover_local_snapshots",
                return_value=self._discovery(root),
            ), contextlib.redirect_stdout(stdout):
                status = main(["analyze", str(root), "--json"])

            payload = json.loads(stdout.getvalue())
            snapshots = payload["local_snapshots"]
            self.assertEqual(status, 0)
            self.assertTrue(snapshots["checked"])
            self.assertEqual(snapshots["count"], 1)
            self.assertFalse(snapshots["size_known"])
            self.assertIsNone(snapshots["size_bytes"])
            self.assertFalse(snapshots["deletion_supported"])


if __name__ == "__main__":
    unittest.main()
