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
from types import SimpleNamespace
from unittest import mock

from openclean.application_languages import (
    discover_preferred_languages,
    scan_application_languages,
)
from openclean.cleanup import SelectionError, select_cleanup_items
from openclean.cli import main
from openclean.engine import IgnoreRules, scan_domains
from openclean.models import Item, ScanResult
from openclean.scanpoints import DOMAINS, SYSTEM_JUNK, ScanPoint


def _make_application(
    root: Path,
    name: str = "Example.app",
    *,
    development_region: str = "en",
) -> tuple[Path, Path]:
    application = root / name
    contents = application / "Contents"
    resources = contents / "Resources"
    resources.mkdir(parents=True)
    (contents / "Info.plist").write_bytes(
        plistlib.dumps(
            {"CFBundleDevelopmentRegion": development_region},
            fmt=plistlib.FMT_BINARY,
        )
    )
    return application, resources


def _make_localization(
    resources: Path,
    language: str,
    files: dict[str, bytes] | None = None,
) -> Path:
    localization = resources / f"{language}.lproj"
    localization.mkdir()
    for name, content in (files or {"Localizable.strings": b"value"}).items():
        target = localization / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return localization


def _with_dataless_flag(stat_result):
    return SimpleNamespace(
        st_mode=stat_result.st_mode,
        st_size=stat_result.st_size,
        st_blocks=stat_result.st_blocks,
        st_mtime=stat_result.st_mtime,
        st_dev=stat_result.st_dev,
        st_ino=stat_result.st_ino,
        st_nlink=stat_result.st_nlink,
        st_uid=stat_result.st_uid,
        st_flags=0x40000000,
    )


class LanguagePreferencesTests(unittest.TestCase):
    def test_defaults_export_reads_apple_languages(self) -> None:
        calls = []
        payload = plistlib.dumps(
            {"AppleLanguages": ["zh-Hans-CN", "en-US", "zh-Hans-CN"]}
        )

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, payload, b"")

        discovery = discover_preferred_languages(runner=runner, timeout=1.25)

        self.assertEqual(discovery.languages, ("zh-Hans-CN", "en-US"))
        self.assertEqual(discovery.issues, ())
        self.assertEqual(
            calls[0][0],
            ["/usr/bin/defaults", "export", "NSGlobalDomain", "-"],
        )
        self.assertEqual(calls[0][1]["timeout"], 1.25)
        self.assertTrue(calls[0][1]["capture_output"])
        self.assertFalse(calls[0][1]["check"])

    def test_defaults_failure_and_invalid_plist_are_explicit(self) -> None:
        failed = discover_preferred_languages(
            runner=lambda command, **_: subprocess.CompletedProcess(
                command,
                1,
                b"",
                b"preferences unavailable",
            )
        )
        invalid = discover_preferred_languages(
            runner=lambda command, **_: subprocess.CompletedProcess(
                command,
                0,
                b"not a plist",
                b"",
            )
        )

        self.assertEqual(failed.languages, ())
        self.assertEqual(failed.issues[0].code, "language_preferences_failed")
        self.assertIn("preferences unavailable", failed.issues[0].message)
        self.assertEqual(invalid.languages, ())
        self.assertIn("无法解析", invalid.issues[0].message)

    def test_empty_language_list_is_not_replaced_with_a_guess(self) -> None:
        payload = plistlib.dumps({"AppleLanguages": []})
        discovery = discover_preferred_languages(
            runner=lambda command, **_: subprocess.CompletedProcess(
                command,
                0,
                payload,
                b"",
            )
        )

        self.assertEqual(discovery.languages, ())
        self.assertIn("缺失或为空", discovery.issues[0].message)


class ApplicationLanguagesScanTests(unittest.TestCase):
    def test_dataless_application_and_info_plist_are_not_read(self) -> None:
        for target_kind in ("application", "info"):
            with self.subTest(target=target_kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "Applications"
                application, resources = _make_application(root)
                _make_localization(resources, "de")
                info = application / "Contents" / "Info.plist"
                target = application if target_kind == "application" else info
                target_stat = target.lstat()
                original_lstat = Path.lstat
                original_read_bytes = Path.read_bytes

                def fake_lstat(
                    path: Path,
                    *,
                    original_lstat=original_lstat,
                    target=target,
                    target_stat=target_stat,
                ):
                    stat_result = original_lstat(path)
                    return (
                        _with_dataless_flag(target_stat)
                        if path == target
                        else stat_result
                    )

                def guarded_read_bytes(
                    path: Path,
                    *,
                    info=info,
                    original_read_bytes=original_read_bytes,
                ):
                    if path == info:
                        raise AssertionError("dataless Info.plist must not be read")
                    return original_read_bytes(path)

                with mock.patch(
                    "openclean.models.MACOS_SF_DATALESS", 0x40000000
                ), mock.patch.object(Path, "lstat", new=fake_lstat), mock.patch.object(
                    Path, "read_bytes", new=guarded_read_bytes
                ):
                    result = scan_application_languages(
                        [root],
                        IgnoreRules(),
                        preferred_languages=["en"],
                    )

                self.assertEqual(result.items, [])
                self.assertIn(
                    "dataless_object_skipped",
                    {issue.code for issue in result.issues},
                )

    def test_dataless_resources_and_localization_are_not_enumerated(self) -> None:
        for target_kind in ("resources", "localization"):
            with self.subTest(target=target_kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "Applications"
                _, resources = _make_application(root)
                localization = _make_localization(resources, "de")
                target = resources if target_kind == "resources" else localization
                target_stat = target.lstat()
                original_lstat = Path.lstat
                original_scandir = os.scandir

                def fake_lstat(
                    path: Path,
                    *,
                    original_lstat=original_lstat,
                    target=target,
                    target_stat=target_stat,
                ):
                    stat_result = original_lstat(path)
                    return (
                        _with_dataless_flag(target_stat)
                        if path == target
                        else stat_result
                    )

                def guarded_scandir(
                    path,
                    *,
                    target=target,
                    original_scandir=original_scandir,
                ):
                    if Path(path) == target:
                        raise AssertionError("dataless directory must not be enumerated")
                    return original_scandir(path)

                with mock.patch(
                    "openclean.models.MACOS_SF_DATALESS", 0x40000000
                ), mock.patch.object(Path, "lstat", new=fake_lstat), mock.patch(
                    "openclean.application_languages.os.scandir",
                    side_effect=guarded_scandir,
                ):
                    result = scan_application_languages(
                        [root],
                        IgnoreRules(),
                        preferred_languages=["en"],
                    )

                self.assertEqual(result.items, [])
                self.assertIn(
                    "dataless_object_skipped",
                    {issue.code for issue in result.issues},
                )

    def test_reports_only_unpreferred_string_only_localizations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Applications"
            application, resources = _make_application(root)
            _make_localization(resources, "Base")
            _make_localization(resources, "en")
            _make_localization(resources, "zh-Hans")
            _make_localization(
                resources,
                "de",
                {
                    "Localizable.strings": b"german",
                    "Plural.stringsdict": b"plural",
                },
            )
            _make_localization(
                resources,
                "fr",
                {"Localizable.strings": b"french", "icon.png": b"png"},
            )
            _make_localization(
                resources,
                "es",
                {"Main.storyboardc/content": b"compiled interface"},
            )
            checkpoints: list[None] = []
            progress: list[None] = []

            result = scan_application_languages(
                [root],
                IgnoreRules(),
                preferred_languages=["zh-Hans-CN"],
                context_note="保守语言审计",
                checkpoint=lambda: checkpoints.append(None),
                on_progress=lambda: progress.append(None),
            )

            self.assertTrue(result.complete)
            self.assertEqual([item.path for item in result.items], [application])
            item = result.items[0]
            self.assertEqual(item.category, "应用语言包")
            self.assertEqual(item.safety, "critical")
            self.assertFalse(item.actionable)
            self.assertFalse(item.preselected)
            self.assertEqual(item.artifact_name, "de")
            self.assertEqual(item.total_count, 1)
            self.assertGreater(item.size, 0)
            self.assertEqual(item.size, item.allocated_size)
            self.assertGreater(item.logical_size or 0, 0)
            self.assertIn("代码签名", item.action_block_reason)
            self.assertIn("保守语言审计", item.note)
            self.assertIn("候选语言（1）：de", item.note)
            self.assertGreater(len(checkpoints), 0)
            self.assertGreater(len(progress), 0)

    def test_same_base_language_and_development_region_are_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Applications"
            application, resources = _make_application(
                root,
                development_region="de",
            )
            _make_localization(resources, "pt-PT")
            candidate = _make_localization(resources, "de")
            candidate = _make_localization(resources, "ja")

            result = scan_application_languages(
                [root],
                IgnoreRules(),
                preferred_languages=["pt-BR"],
            )

            self.assertEqual([item.path for item in result.items], [application])
            self.assertEqual(result.items[0].artifact_name, candidate.stem)

    def test_application_in_one_container_directory_is_discovered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Applications"
            application, resources = _make_application(root / "Vendor")
            candidate = _make_localization(resources, "de")

            result = scan_application_languages(
                [root],
                IgnoreRules(),
                preferred_languages=["en"],
            )

            self.assertEqual([item.path for item in result.items], [application])
            self.assertEqual(result.items[0].artifact_name, candidate.stem)

    def test_ignored_candidate_or_descendant_blocks_whole_language_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Applications"
            _, resources = _make_application(root)
            candidate = _make_localization(resources, "de")
            child = candidate / "Localizable.strings"

            ignored_root = scan_application_languages(
                [root],
                IgnoreRules([str(candidate)]),
                preferred_languages=["en"],
            )
            ignored_child = scan_application_languages(
                [root],
                IgnoreRules([str(child)]),
                preferred_languages=["en"],
            )

            self.assertEqual(ignored_root.items, [])
            self.assertEqual(ignored_child.items, [])

    def test_invalid_application_metadata_is_reported_and_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Applications"
            application, resources = _make_application(root)
            (application / "Contents" / "Info.plist").write_bytes(b"invalid")
            _make_localization(resources, "de")

            result = scan_application_languages(
                [root],
                IgnoreRules(),
                preferred_languages=["en"],
            )

            self.assertFalse(result.complete)
            self.assertEqual(result.items, [])
            self.assertEqual(result.issues[0].code, "application_metadata_invalid")

    def test_missing_application_metadata_is_not_assumed_to_be_english(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Applications"
            application, resources = _make_application(root)
            (application / "Contents" / "Info.plist").unlink()
            _make_localization(resources, "de")

            result = scan_application_languages(
                [root],
                IgnoreRules(),
                preferred_languages=["en"],
            )

            self.assertFalse(result.complete)
            self.assertEqual(result.items, [])
            self.assertEqual(result.issues[0].code, "application_metadata_invalid")
            self.assertIn("缺失", result.issues[0].message)

    def test_missing_development_region_is_not_assumed_to_be_english(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Applications"
            application, resources = _make_application(root)
            (application / "Contents" / "Info.plist").write_bytes(
                plistlib.dumps({}, fmt=plistlib.FMT_BINARY)
            )
            _make_localization(resources, "de")

            result = scan_application_languages(
                [root],
                IgnoreRules(),
                preferred_languages=["en"],
            )

            self.assertFalse(result.complete)
            self.assertEqual(result.items, [])
            self.assertEqual(result.issues[0].code, "application_metadata_invalid")
            self.assertIn("CFBundleDevelopmentRegion", result.issues[0].message)

    def test_missing_preferences_skips_scan_instead_of_guessing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Applications"
            _, resources = _make_application(root)
            _make_localization(resources, "de")
            progress: list[None] = []

            result = scan_application_languages(
                [root],
                IgnoreRules(),
                preferences_runner=lambda command, **_: subprocess.CompletedProcess(
                    command,
                    1,
                    b"",
                    b"unavailable",
                ),
                on_progress=lambda: progress.append(None),
            )

            self.assertFalse(result.complete)
            self.assertEqual(result.items, [])
            self.assertEqual(progress, [])
            self.assertEqual(result.issues[0].code, "language_preferences_failed")

    def test_read_only_candidate_cannot_be_selected_for_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Applications"
            _, resources = _make_application(root)
            _make_localization(resources, "de")
            item = scan_application_languages(
                [root],
                IgnoreRules(),
                preferred_languages=["en"],
            ).items[0]

            with self.assertRaisesRegex(SelectionError, "代码签名"):
                select_cleanup_items(
                    [item],
                    selectors=[str(item.path)],
                    include_critical=True,
                )


class ApplicationLanguagesIntegrationTests(unittest.TestCase):
    def test_system_scan_point_is_critical_dynamic_audit(self) -> None:
        point = next(
            point for point in SYSTEM_JUNK if point.category == "应用语言包"
        )

        self.assertEqual(point.scanner, "application-languages")
        self.assertEqual(point.safety, "critical")
        self.assertEqual(
            point.paths,
            ("/Applications", "/System/Applications", "~/Applications"),
        )

    def test_engine_assigns_system_domain_to_dynamic_results(self) -> None:
        point = ScanPoint(
            "应用语言包",
            ("/Applications",),
            "critical",
            scanner="application-languages",
        )
        item = Item(
            path=Path("/Applications/Example.app/Contents/Resources/de.lproj"),
            size=4096,
            category="应用语言包",
            safety="critical",
            actionable=False,
            action_block_reason="只读",
        )
        scanner_result = ScanResult(items=[item])

        with mock.patch.dict(DOMAINS, {"system": [point]}), mock.patch(
            "openclean.engine.scan_application_languages",
            return_value=scanner_result,
        ) as scanner:
            result = scan_domains(["system"], workers=1)

        self.assertTrue(result.complete)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].domain, "system")
        self.assertFalse(result.items[0].actionable)
        scanner.assert_called_once()
        self.assertEqual(scanner.call_args.args[0], point.paths)
        self.assertEqual(scanner.call_args.kwargs["category"], "应用语言包")

    def test_clean_junk_json_keeps_audit_locked_and_unselected(self) -> None:
        point = ScanPoint(
            "应用语言包",
            ("/Applications",),
            "critical",
            scanner="application-languages",
        )
        item = Item(
            path=Path("/Applications/Example.app"),
            size=8192,
            category="应用语言包",
            safety="critical",
            actionable=False,
            action_block_reason="代码签名风险，只读审计",
            total_count=2,
            preselected=False,
        )
        scanner_result = ScanResult(items=[item])

        with tempfile.TemporaryDirectory() as tmp:
            rules = Path(tmp) / "rules.json"
            rules.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
            stdout = io.StringIO()
            with mock.patch.dict(DOMAINS, {"system": [point]}), mock.patch(
                "openclean.engine.scan_application_languages",
                return_value=scanner_result,
            ), contextlib.redirect_stdout(stdout):
                status = main(
                    ["clean", "junk", "--rules", str(rules), "--json"]
                )

        payload = json.loads(stdout.getvalue())
        audit = payload["categories"][0]["items"][0]
        self.assertEqual(status, 0)
        self.assertEqual(payload["mode"], "preview")
        self.assertIsNone(payload["cleanup"])
        self.assertEqual(audit["category"], "应用语言包")
        self.assertEqual(audit["total_count"], 2)
        self.assertEqual(audit["potential_bytes"], 8192)
        self.assertEqual(audit["reclaimable_bytes"], 0)
        self.assertEqual(payload["potential_bytes"], 8192)
        self.assertEqual(payload["reclaimable_bytes"], 0)
        self.assertFalse(audit["actionable"])
        self.assertFalse(audit["preselected"])


if __name__ == "__main__":
    unittest.main()
