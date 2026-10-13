from __future__ import annotations

import contextlib
import io
import json
import os
import plistlib
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openclean.application_binaries import read_macho_slices, scan_application_binaries
from openclean.application_languages import scan_application_languages
from openclean.cli import _print_report
from openclean.cleanup import SelectionError, select_cleanup_items
from openclean.engine import Cancelled, IgnoreRules, finalize_overlapping_result
from openclean import engine
from openclean.models import FileFacts, Item, ScanResult
from openclean.runtime.finding_projection import item_from_payload, item_to_payload
from openclean.tui import item_detail_lines

ARM = 0x100000C
X86 = 0x1000007


def thin(cpu=ARM, subtype=0):
    return struct.pack("<IiiIIIII", 0xFEEDFACF, cpu, subtype, 2, 0, 0, 0, 0)


def fat(endian=">", wide=False):
    magic = 0xCAFEBABF if wide else 0xCAFEBABE
    header = struct.pack(endian + "II", magic, 2)
    layout = endian + ("iiQQII" if wide else "iiIII")
    for cpu, offset in ((ARM, 1024), (X86, 2048)):
        values = (cpu, 0, offset, 32, 10) + ((0,) if wide else ())
        header += struct.pack(layout, *values)
    data = bytearray(2080)
    data[: len(header)] = header
    data[1024:1056] = thin(ARM)
    data[2048:2080] = thin(X86)
    return bytes(data)


def app(root, name="Example.app", development="en"):
    path = root / name
    resources = path / "Contents/Resources"
    resources.mkdir(parents=True)
    metadata = {} if development is None else {"CFBundleDevelopmentRegion": development}
    (path / "Contents/Info.plist").write_bytes(plistlib.dumps(metadata))
    return path, resources


class MachOTests(unittest.TestCase):
    def parse(self, directory, content):
        path = Path(directory) / "binary"
        path.write_bytes(content)
        with path.open("rb") as stream:
            return read_macho_slices(stream.fileno(), len(content))

    def test_thin_and_four_fat_layouts(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.parse(tmp, thin())[0]["architecture"], "arm64")
            for endian in (">", "<"):
                for wide in (False, True):
                    with self.subTest(endian=endian, wide=wide):
                        slices = self.parse(tmp, fat(endian, wide))
                        self.assertEqual(
                            [s["architecture"] for s in slices], ["arm64", "x86_64"]
                        )
                        self.assertEqual(sum(s["logical_bytes"] for s in slices), 64)

    def test_corrupt_tables_never_report_capacity(self):
        with tempfile.TemporaryDirectory() as tmp:
            cases = [fat()[:60], struct.pack(">II", 0xCAFEBABE, 65)]
            overlap = bytearray(fat())
            struct.pack_into(">I", overlap, 36, 1024)
            cases.append(overlap)
            wrong_cpu = bytearray(fat())
            struct.pack_into("<i", wrong_cpu, 2052, ARM)
            cases.append(wrong_cpu)
            for data in cases:
                with self.subTest(length=len(data)), self.assertRaises(ValueError):
                    self.parse(tmp, data)
            self.assertEqual(self.parse(tmp, b"ordinary document"), [])

    def test_fat_static_archives_preserve_table_only_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = bytearray(fat())
            data[1024:1032] = b"!<arch>\n"
            data[2048:2056] = b"!<arch>\n"
            records = self.parse(tmp, data)
            self.assertEqual(
                [r["slice_format"] for r in records], ["ar_archive", "ar_archive"]
            )
            self.assertTrue(
                all(r["architecture_evidence"] == "fat_table_only" for r in records)
            )


class ResourceScanTests(unittest.TestCase):
    def test_nested_language_directory_is_checked_before_starting_its_walk(self):
        with tempfile.TemporaryDirectory() as tmp:
            application, resources = app(Path(tmp).resolve())
            foreign = resources / "fr.lproj"
            local = resources / "de.lproj"
            for directory in (foreign, local):
                directory.mkdir()
                (directory / "data.pak").write_bytes(b"localized data")

            def filesystem(path):
                return 2 if Path(path) == foreign or foreign in Path(path).parents else 1

            with mock.patch("openclean.resource_walk.filesystem_id_retry", side_effect=filesystem):
                result = scan_application_languages(
                    [application], IgnoreRules(), preferred_languages=["en"], include_resources=True,
                )
            self.assertEqual(result.items[0].artifact_name, "de")
            self.assertEqual([r["path"] for r in result.diagnostics[0]["localizations"]], [str(local)])
            self.assertTrue(any(i.code == "cross_filesystem_skipped" and i.path == foreign
                                for i in result.issues))

    def test_dynamic_cancel_preserves_completed_diagnostics(self):
        from openclean.progress import ProgressTaskSpec, WeightedProgress
        from openclean.scanpoints import ScanPoint

        with tempfile.TemporaryDirectory() as tmp:
            ctl = engine.Control()
            partial = ScanResult(
                cancelled=True,
                diagnostics=[
                    {"kind": "application_binaries", "anchor_path": str(Path(tmp))}
                ],
            )

            def cancelled_scan(*args, **kwargs):
                ctl.cancel()
                return partial

            progress = WeightedProgress([ProgressTaskSpec("binary", "binary", 1)])
            point = ScanPoint("binary", (), scanner="application-binaries")
            with mock.patch(
                "openclean.engine.scan_application_binaries", side_effect=cancelled_scan
            ):
                result = engine._scan_dynamic_point_with_progress(
                    point, ctl, IgnoreRules(), progress.task("binary"), mock.Mock()
                )
            self.assertTrue(result.cancelled)
            self.assertEqual(result.diagnostics, partial.diagnostics)

    def test_cloud_header_is_never_opened_and_unknown_host_stays_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, resources = app(root)
            hidden = resources / "cloud"
            hidden.write_bytes(fat())
            (resources / "local").write_bytes(fat())
            original = FileFacts.is_probable_cloud_placeholder.fget
            with (
                mock.patch.object(
                    FileFacts,
                    "is_probable_cloud_placeholder",
                    property(lambda facts: facts.path == hidden or original(facts)),
                ),
                mock.patch(
                    "openclean.application_binaries.native_cpu_type", return_value=None
                ),
            ):
                result = scan_application_binaries([root], IgnoreRules())
            self.assertEqual(result.items[0].total_count, 1)
            self.assertIsNone(result.items[0].logical_size)
            self.assertNotIn(str(hidden), json.dumps(result.diagnostics))
            self.assertIn("dataless_object_skipped", {i.code for i in result.issues})

    def test_binary_subset_and_language_coexist_with_exact_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            application, resources = app(root)
            binary = resources / "universal"
            binary.write_bytes(fat())
            locale = (
                application
                / "Contents/Frameworks/F.framework/Versions/A/Resources/de.lproj"
            )
            locale.mkdir(parents=True)
            (locale / "locale.pak").write_bytes(b"localized data")
            languages = scan_application_languages(
                [root],
                IgnoreRules(),
                preferred_languages=["en"],
                include_resources=True,
            )
            binaries = scan_application_binaries([root], IgnoreRules(), native_cpu=ARM)
            self.assertTrue(languages.complete)
            self.assertTrue(binaries.complete)
            self.assertEqual(binaries.items[0].logical_size, 32)
            self.assertEqual(binaries.items[0].size, 0)
            self.assertEqual(languages.items[0].artifact_name, "de")
            self.assertEqual(
                languages.diagnostics[0]["localizations"][0]["resource_types"], [".pak"]
            )
            combined = ScanResult()
            combined.extend(languages)
            combined.extend(binaries)
            final = finalize_overlapping_result(combined)
            self.assertEqual(len(final.items), 2)
            self.assertEqual(len(final.diagnostics), 2)
            self.assertEqual(final.total, languages.total)
            self.assertEqual(final.actionable_total, 0)
            for item in final.items:
                self.assertFalse(item.preselected)
                with self.assertRaises(SelectionError):
                    select_cleanup_items(
                        [item], selectors=[str(item.path)], include_critical=True
                    )
            self.assertIn(
                "其它 CPU 架构逻辑量", "\n".join(item_detail_lines(binaries.items[0]))
            )
            self.assertEqual(binary.read_bytes(), fat())

    def test_missing_development_language_is_reported_without_guessing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            application, resources = app(root, development=None)
            for name in ("Base", "zh-Hans", "en", "de"):
                locale = resources / (name + ".lproj")
                locale.mkdir()
                (locale / "mixed.bin").write_bytes(b"content")
            result = scan_application_languages(
                [root],
                IgnoreRules(),
                preferred_languages=["zh-Hans"],
                include_resources=True,
            )
            self.assertTrue(result.complete)
            self.assertEqual(result.items[0].artifact_name, "de,en")
            self.assertIsNone(result.diagnostics[0]["development_language"])
            self.assertFalse(result.items[0].actionable)

    def test_link_ignore_and_hardlink_do_not_duplicate_or_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            application, resources = app(root)
            outside = root / "outside"
            outside.write_bytes(fat())
            (resources / "linked").symlink_to(outside)
            binary = resources / "binary"
            binary.write_bytes(fat())
            os.link(binary, resources / "alias")
            ignored = resources / "ignored"
            ignored.write_bytes(fat())
            result = scan_application_binaries(
                [root], IgnoreRules([str(ignored)]), native_cpu=ARM
            )
            self.assertEqual(result.items[0].total_count, 1)
            self.assertEqual(result.items[0].logical_size, 32)
            self.assertEqual(len(result.diagnostics[0]["files"]), 1)

    def test_limit_and_cancel_do_not_claim_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            application, resources = app(root)
            (resources / "binary").write_bytes(fat())
            result = scan_application_binaries(
                [root], IgnoreRules(), native_cpu=ARM, max_entries=1
            )
            self.assertFalse(result.complete)
            self.assertIn("scan_limit_reached", {i.code for i in result.issues})

            def cancel():
                raise Cancelled()

            result = scan_application_binaries([root], IgnoreRules(), checkpoint=cancel)
            self.assertTrue(result.cancelled)

    def test_json_details_are_redacted_and_old_item_snapshot_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            application, resources = app(root)
            (resources / "binary").write_bytes(fat())
            result = scan_application_binaries([root], IgnoreRules(), native_cpu=ARM)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                _print_report(result, True, ["system"], redact_paths=True)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["schema_version"], 2)
            self.assertNotIn(str(root), output.getvalue())
            self.assertTrue(
                payload["diagnostics"][0]["anchor_path"].startswith("path:")
            )
            old = Item(root / "cache", 10, "cache")
            self.assertEqual(item_from_payload(item_to_payload(old)), old)

    def test_binary_metadata_failure_is_partial_not_task_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            application, resources = app(root)
            (resources / "bad").write_bytes(fat()[:50])
            (resources / "good").write_bytes(fat())
            result = scan_application_binaries([root], IgnoreRules(), native_cpu=ARM)
            self.assertEqual(len(result.items), 1)
            self.assertFalse(result.complete)
            self.assertFalse(result.diagnostics[0]["complete"])


if __name__ == "__main__":
    unittest.main()
