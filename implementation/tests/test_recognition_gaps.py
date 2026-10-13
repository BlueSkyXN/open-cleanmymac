from __future__ import annotations

import contextlib
import io
import json
import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openclean.application_updates import scan_chrome_versions
from openclean.cli import _diagnostic_payload, main
from openclean.cleanup import (
    SelectionError,
    select_cleanup_items,
    with_cleanup_selection,
)
from openclean.disk_images import image_mount_state
from openclean.downloads import scan_downloads
from openclean.engine import IgnoreRules, finalize_overlapping_result
from openclean.models import ScanIssue, ScanResult


def chrome(root, current="2.0", old="1.0"):
    application = root / "Chrome.app"
    contents = application / "Contents"
    contents.mkdir(parents=True)
    (contents / "Info.plist").write_bytes(
        plistlib.dumps(
            {
                "CFBundleIdentifier": "com.google.Chrome",
                "CFBundleShortVersionString": current,
            }
        )
    )
    versions = contents / "Frameworks/Google Chrome Framework.framework/Versions"
    for version in (current, old):
        resources = versions / version / "Resources"
        resources.mkdir(parents=True)
        (resources / "Info.plist").write_bytes(
            plistlib.dumps(
                {
                    "CFBundleIdentifier": "com.google.Chrome.framework",
                    "CFBundleShortVersionString": version,
                }
            )
        )
        (resources / "binary").write_bytes(b"component")
    (versions / "Current").symlink_to(current)
    return application, versions


def no_mounts(*args, **kwargs):
    return subprocess.CompletedProcess(args[0], 0, plistlib.dumps({"images": []}), b"")


def format_result(*args, **kwargs):
    return subprocess.CompletedProcess(args[0], 0, "DOS/MBR boot sector\n", "")


class UpdateRecognitionTests(unittest.TestCase):
    def test_old_component_has_version_evidence_but_no_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            application, versions = chrome(Path(tmp))
            result = scan_chrome_versions([application], IgnoreRules())
            self.assertTrue(result.complete)
            self.assertEqual(len(result.items), 1)
            self.assertEqual(
                result.diagnostics[0]["updater_status"], "older_version_residue"
            )
            self.assertEqual(result.diagnostics[0]["installed_version"], "2.0")
            self.assertEqual(result.diagnostics[0]["staged_version"], "1.0")
            self.assertGreater(result.items[0].resource_total_size, 0)
            self.assertEqual(result.total, 0)
            with self.assertRaises(SelectionError):
                select_cleanup_items(
                    result.items,
                    selectors=[str(versions / "1.0")],
                    include_critical=True,
                )
            selected = with_cleanup_selection(result, [])
            self.assertEqual(selected.diagnostics, result.diagnostics)

    def test_version_directory_name_alone_is_not_old_update_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            application, versions = chrome(Path(tmp))
            (versions / "1.0/Resources/Info.plist").write_bytes(
                plistlib.dumps({"CFBundleIdentifier": "wrong"})
            )
            result = scan_chrome_versions([application], IgnoreRules())
            self.assertFalse(result.complete)
            self.assertEqual(result.diagnostics[0]["updater_status"], "version_unknown")

    def test_current_pointer_mismatch_never_reports_old_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            application, versions = chrome(Path(tmp))
            (versions / "Current").unlink()
            (versions / "Current").symlink_to("1.0")
            result = scan_chrome_versions([application], IgnoreRules())
            self.assertFalse(result.complete)
            self.assertTrue(
                all(
                    d["updater_status"] == "version_unknown" for d in result.diagnostics
                )
            )

    def test_ignored_old_component_is_not_measured(self):
        with tempfile.TemporaryDirectory() as tmp:
            application, versions = chrome(Path(tmp))
            result = scan_chrome_versions(
                [application], IgnoreRules([str(versions / "1.0")])
            )
            self.assertTrue(result.complete)
            self.assertEqual(result.items, [])


class DownloadRecognitionTests(unittest.TestCase):
    def test_format_inspection_uses_open_file_descriptor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "sample.img").write_bytes(b"image metadata")
            calls = []

            def inspect(command, **kwargs):
                descriptor = kwargs["pass_fds"][0]
                self.assertEqual(command[-1], f"/dev/fd/{descriptor}")
                self.assertEqual(os.pread(descriptor, 14, 0), b"image metadata")
                calls.append(command)
                return format_result(command)

            result = scan_downloads(
                [root], IgnoreRules(), mount_runner=no_mounts, format_runner=inspect
            )
            self.assertTrue(result.complete)
            self.assertEqual(len(calls), 1)

    def test_file_root_reaches_scanner_and_preserves_diagnostic_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            result = ScanResult(
                diagnostics=[{"kind": "downloads", "anchor_path": str(root)}]
            )
            output = io.StringIO()
            with (
                mock.patch("openclean.cli.scan_domains", return_value=result) as scan,
                contextlib.redirect_stdout(output),
            ):
                status = main(
                    ["scan", "--domain", "system", "--file-root", str(root), "--json"]
                )
            self.assertEqual(status, 0)
            self.assertEqual(scan.call_args.kwargs["file_roots"], [root])
            self.assertEqual(
                json.loads(output.getvalue())["diagnostics"], result.diagnostics
            )

    def test_small_downloads_and_test_images_are_readonly_and_counted_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "report.docx").write_bytes(b"document")
            (root / "archive.zip").write_bytes(b"archive")
            (root / "partial.crdownload").write_bytes(b"in progress")
            tests = root / "testdata"
            tests.mkdir()
            image = tests / "mbr.img"
            image.write_bytes(b"image fixture")
            os.link(image, tests / "alias.img")
            (root / "link.img").symlink_to(image)
            with mock.patch("openclean.disk_images.subprocess.run") as run:
                result = scan_downloads(
                    [root, tests],
                    IgnoreRules(),
                    mount_runner=no_mounts,
                    format_runner=format_result,
                )
                run.assert_not_called()
            final = finalize_overlapping_result(result)
            self.assertTrue(final.complete)
            self.assertEqual(len(final.items), 2)
            records = {d["kind"]: d for d in final.diagnostics}
            self.assertEqual(len(records["disk_image"]["files"]), 1)
            found = records["disk_image"]["files"][0]
            self.assertEqual(found["context"], "development_files")
            self.assertEqual(found["usage"], "unknown")
            self.assertEqual(found["mount_state"], "not_mounted")
            self.assertEqual(
                {f["file_type"] for f in records["downloads"]["files"]},
                {"other", "archive", "in_progress"},
            )
            self.assertEqual(final.total, 0)
            self.assertEqual(final.actionable_total, 0)
            self.assertTrue(all(not i.preselected for i in final.items))

    def test_unknown_mount_schema_never_becomes_unmounted(self):
        path = Path("/private/example.img")
        self.assertEqual(image_mount_state(path, None), "unknown")
        self.assertEqual(image_mount_state(path, []), "not_mounted")
        self.assertEqual(image_mount_state(path, [{"source": str(path)}]), "mounted")
        self.assertEqual(
            image_mount_state(path, [{"new-schema": "unrecognized"}]), "unknown"
        )

    def test_budget_is_reported_without_discarding_completed_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            for n in range(3):
                (root / f"{n}.txt").write_text("fixture")
            result = scan_downloads([root], IgnoreRules(), max_files=2)
            self.assertFalse(result.complete)
            self.assertEqual(len(result.diagnostics[0]["files"]), 2)

    def test_file_root_cli_validation_precedes_scan(self):
        output = io.StringIO()
        with (
            mock.patch("openclean.cli.scan_domains") as scan,
            contextlib.redirect_stdout(output),
        ):
            status = main(
                ["scan", "--domain", "ai", "--file-root", "/private/example", "--json"]
            )
        self.assertEqual(status, 2)
        scan.assert_not_called()
        self.assertEqual(
            json.loads(output.getvalue())["error"]["code"], "invalid_option_combination"
        )

    def test_permission_report_does_not_guess_full_disk_access(self):
        result = ScanResult(
            issues=[
                ScanIssue(
                    "permission_denied", "fixture", "cache", Path("/private/example")
                )
            ]
        )
        record = _diagnostic_payload(result)[0]
        self.assertEqual(record["unreadable_path_count"], 1)
        self.assertEqual(record["failures"][0]["cause"], "undetermined")
        self.assertFalse(record["complete"])


if __name__ == "__main__":
    unittest.main()
