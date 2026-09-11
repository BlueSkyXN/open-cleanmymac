from __future__ import annotations

import os
import plistlib
import subprocess
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest import mock

from openclean.cleanup import SelectionError, execute_cleanup, select_cleanup_items
from openclean.engine import IgnoreRules, finalize_overlapping_result, scan_points
from openclean.models import ScanResult
from openclean.processes import ProcessSnapshot
from openclean.scanpoints import DEVELOPER_JUNK, SYSTEM_JUNK, ScanPoint
from openclean.updater import (
    assess_updater_candidate,
    assess_updater_staging_root,
)


def _write_app(path: Path, bundle_id: str, version: str) -> None:
    contents = path / "Contents"
    contents.mkdir(parents=True, exist_ok=True)
    (contents / "Info.plist").write_bytes(
        plistlib.dumps(
            {
                "CFBundleIdentifier": bundle_id,
                "CFBundleShortVersionString": version,
                "CFBundleVersion": version,
            },
            fmt=plistlib.FMT_BINARY,
        )
    )
    (contents / "payload.bin").write_bytes(b"payload")


def _write_app_zip(path: Path, app_name: str, bundle_id: str, version: str) -> None:
    path.parent.mkdir(parents=True)
    payload = plistlib.dumps(
        {
            "CFBundleIdentifier": bundle_id,
            "CFBundleShortVersionString": version,
            "CFBundleVersion": version,
        },
        fmt=plistlib.FMT_BINARY,
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{app_name}.app/Contents/Info.plist", payload)
        archive.writestr(f"{app_name}.app/Contents/payload.bin", b"payload")


class UpdaterAssessmentTests(unittest.TestCase):
    def test_classifies_new_same_and_old_staged_versions(self) -> None:
        cases = (
            ("5.3.14", "5.3.13", "pending_update", True),
            ("5.3.13.0", "5.3.13", "same_version_residue", False),
            ("5.3.12", "5.3.13", "older_version_residue", False),
        )
        for staged_version, installed_version, status, blocked in cases:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp) / "home"
                applications = home / "Applications"
                installed = applications / "WorkBuddy.app"
                cache = (
                    home
                    / "Library/Caches/com.workbuddy.workbuddy.BundleMigration"
                )
                staged = cache / "extracted/build/WorkBuddy.app"
                _write_app(installed, "com.workbuddy.workbuddy", installed_version)
                _write_app(staged, "com.workbuddy.workbuddy", staged_version)

                assessment = assess_updater_candidate(
                    cache,
                    home=home,
                    application_roots=(applications,),
                )

                self.assertIsNotNone(assessment)
                assert assessment is not None
                self.assertEqual(assessment.status, status)
                self.assertEqual(assessment.blocks_cleanup, blocked)

    def test_missing_installed_application_is_protected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            applications = home / "Applications"
            cache = home / "Library/Caches/com.aliyun.lingma.ide.ShipIt"
            staged = cache / "update.test/Qoder CN IDE.app"
            _write_app(staged, "com.aliyun.lingma.ide", "1.27.0")

            assessment = assess_updater_candidate(
                cache,
                home=home,
                application_roots=(applications,),
            )

            self.assertIsNotNone(assessment)
            assert assessment is not None
            self.assertEqual(assessment.status, "installed_app_missing")
            self.assertTrue(assessment.blocks_cleanup)

    def test_recognizes_bounded_codex_sparkle_installation_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            applications = home / "Applications"
            cache = home / "Library/Caches/com.openai.codex"
            staged = (
                cache
                / "org.sparkle-project.Sparkle/Installation/run/payload/ChatGPT.app"
            )
            _write_app(
                applications / "ChatGPT.app",
                "com.openai.codex",
                "26.825.41651",
            )
            _write_app(staged, "com.openai.codex", "26.825.50000")

            assessment = assess_updater_candidate(
                cache,
                home=home,
                application_roots=(applications,),
            )

            self.assertIsNotNone(assessment)
            assert assessment is not None
            self.assertEqual(assessment.status, "pending_update")
            self.assertTrue(assessment.blocks_cleanup)

    def test_reads_top_level_bundle_metadata_from_zip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            applications = home / "Applications"
            installed = applications / "Qoder CN.app"
            cache = home / "Library/Caches/qoder-cn-updater"
            archive = cache / "pending/Qoder-CN-mac-arm64.zip"
            _write_app(installed, "com.qodercn.app", "0.1.2")
            _write_app_zip(archive, "Qoder CN", "com.qodercn.app", "0.1.2")

            assessment = assess_updater_candidate(
                cache,
                home=home,
                application_roots=(applications,),
            )

            self.assertIsNotNone(assessment)
            assert assessment is not None
            self.assertEqual(assessment.status, "same_version_residue")

    def test_corrupt_staged_archive_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            cache = home / "Library/Caches/qoder-cn-updater"
            archive = cache / "pending/Qoder-CN-mac-arm64.zip"
            archive.parent.mkdir(parents=True)
            archive.write_bytes(b"not a zip")

            assessment = assess_updater_candidate(
                cache,
                home=home,
                application_roots=(home / "Applications",),
            )

            self.assertIsNotNone(assessment)
            assert assessment is not None
            self.assertEqual(assessment.status, "version_unknown")
            self.assertTrue(assessment.blocks_cleanup)

    def test_non_updater_path_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(
                assess_updater_candidate(Path(tmp) / "ordinary-cache")
            )

    def test_dynamic_staging_root_uses_same_fail_closed_version_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging = root / "com.aliyun.lingma.ide.ShipIt.abc"
            _write_app(
                staging / "Qoder CN IDE.app",
                "com.aliyun.lingma.ide",
                "1.27.0",
            )

            assessment = assess_updater_staging_root(
                staging,
                bundle_id="com.aliyun.lingma.ide",
                staged_app_globs=("Qoder CN IDE.app",),
                application_roots=(root / "Applications",),
            )

            self.assertIsNotNone(assessment)
            assert assessment is not None
            self.assertEqual(assessment.status, "installed_app_missing")
            self.assertEqual(assessment.staged_version, "1.27.0")


class UpdaterScanAndCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        for module in ("engine", "cleanup"):
            patcher = mock.patch(f"openclean.{module}.capture_process_snapshot",
                                 return_value=ProcessSnapshot(()))
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_environment_updater_and_merge_retain_risk_and_live_version_checks(self) -> None:
        for include_generic in (False, True):
            for changed in (False, True):
                with self.subTest(generic=include_generic, changed=changed), tempfile.TemporaryDirectory() as tmp:
                    home, _, cache = self._fixture(Path(tmp).resolve(), "1.0", "1.0")
                    uv = replace(next(p for p in DEVELOPER_JUNK if p.category == "uv 缓存"), domain="developer")
                    generic = replace(next(p for p in SYSTEM_JUNK if p.category == "用户缓存"), domain="system")
                    points = [generic, uv] if include_generic else [uv]
                    trash = home / ".Trash"
                    trash.mkdir(mode=0o700)
                    with mock.patch.dict(os.environ, {"HOME": str(home), "UV_CACHE_DIR": str(cache)}), mock.patch(
                        "openclean.updater._application_roots", return_value=(home / "Applications",),
                    ), mock.patch("openclean.engine.capture_process_snapshot", return_value=ProcessSnapshot(())):
                        result = finalize_overlapping_result(scan_points(points, workers=1))
                        item = result.items[0]
                        self.assertEqual(item.updater_status, "same_version_residue")
                        self.assertEqual(item.safety, "critical")
                        self.assertTrue(item.requires_explicit_selection)
                        self.assertEqual((item.installed_version, item.staged_version), ("1.0", "1.0"))
                        with self.assertRaises(SelectionError):
                            select_cleanup_items(result.items, selectors=[str(cache)], include_confirm=True)
                        selected = select_cleanup_items(result.items, selectors=[str(cache)], include_critical=True)
                        if changed:
                            _write_app(cache / "extracted/build/WorkBuddy.app", "com.workbuddy.workbuddy", "2.0")
                        report = execute_cleanup(
                            selected, IgnoreRules(), home=home, trash_resolver=lambda _: trash,
                            process_runner=lambda command, **_: subprocess.CompletedProcess(command, 0, "", ""),
                        )
                    self.assertEqual(report.complete, not changed)
                    self.assertEqual(cache.exists(), changed)
                    if changed:
                        self.assertIn("版本状态已变化", report.outcomes[0].message)

    def test_same_path_merge_preserves_updater_metadata_and_blocks_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, cache_root, cache = self._fixture(Path(tmp).resolve(), "1.0", "1.0")
            point = ScanPoint("generic", (str(cache_root),), "confirm", expand_children=True,
                              updater_protection=True, domain="system")
            with mock.patch.dict(os.environ, {"HOME": str(home)}), mock.patch(
                "openclean.updater._application_roots", return_value=(home / "Applications",),
            ):
                updater = scan_points([point], workers=1).items[0]
            plain = replace(updater, category="specific", domain="developer", safety="safe",
                            updater_status="", installed_version="", staged_version="",
                            updater_external_install=False, requires_explicit_selection=False)
            conflict = replace(updater, staged_version="0.9", updater_status="older_version_residue")
            for other in (plain, conflict):
                for items in ([updater, other], [other, updater]):
                    with self.subTest(other=other.category, order=items[0].category):
                        merged = finalize_overlapping_result(ScanResult(items=items)).items[0]
                        self.assertEqual(merged.safety, "critical")
                        self.assertTrue(merged.requires_explicit_selection)
                        self.assertTrue(merged.updater_status)
                        if other is plain:
                            self.assertEqual((merged.updater_status, merged.installed_version, merged.staged_version),
                                             ("same_version_residue", "1.0", "1.0"))
                        else:
                            self.assertFalse(merged.actionable)
                            self.assertIn("不一致", merged.action_block_reason)

    def _fixture(self, root: Path, staged_version: str, installed_version: str):
        home = root / "home"
        applications = home / "Applications"
        cache_root = home / "Library/Caches"
        cache = cache_root / "com.workbuddy.workbuddy.BundleMigration"
        _write_app(
            applications / "WorkBuddy.app",
            "com.workbuddy.workbuddy",
            installed_version,
        )
        _write_app(
            cache / "extracted/build/WorkBuddy.app",
            "com.workbuddy.workbuddy",
            staged_version,
        )
        return home, cache_root, cache

    def test_pending_update_is_visible_but_non_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, cache_root, cache = self._fixture(
                Path(tmp), "5.3.14", "5.3.13"
            )
            point = ScanPoint(
                "用户缓存",
                (str(cache_root),),
                "confirm",
                expand_children=True,
                updater_protection=True,
            )

            with mock.patch.dict("os.environ", {"HOME": str(home)}), mock.patch(
                "openclean.updater._application_roots",
                return_value=(home / "Applications",),
            ):
                result = scan_points([point], workers=1)

            self.assertEqual(len(result.items), 1)
            item = result.items[0]
            self.assertEqual(item.path, cache)
            self.assertEqual(item.updater_status, "pending_update")
            self.assertEqual(item.safety, "critical")
            self.assertTrue(item.requires_explicit_selection)
            self.assertFalse(item.actionable)
            self.assertIn("尚未安装", item.action_block_reason)

    def test_execution_rechecks_updater_version_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, cache_root, cache = self._fixture(
                Path(tmp), "5.3.13", "5.3.13"
            )
            point = ScanPoint(
                "用户缓存",
                (str(cache_root),),
                "confirm",
                expand_children=True,
                updater_protection=True,
            )
            with mock.patch.dict("os.environ", {"HOME": str(home)}), mock.patch(
                "openclean.updater._application_roots",
                return_value=(home / "Applications",),
            ):
                result = scan_points([point], workers=1)
            item = result.items[0]
            self.assertTrue(item.actionable)
            self.assertEqual(item.updater_status, "same_version_residue")

            _write_app(
                cache / "extracted/build/WorkBuddy.app",
                "com.workbuddy.workbuddy",
                "5.3.14",
            )
            with mock.patch.dict("os.environ", {"HOME": str(home)}), mock.patch(
                "openclean.updater._application_roots",
                return_value=(home / "Applications",),
            ):
                report = execute_cleanup(
                    [item],
                    IgnoreRules(),
                    home=home,
                    uid=os.getuid(),
                )

            self.assertFalse(report.complete)
            self.assertEqual(report.outcomes[0].status, "blocked")
            self.assertIn("版本状态已变化", report.outcomes[0].message)
            self.assertTrue(cache.exists())


if __name__ == "__main__":
    unittest.main()
