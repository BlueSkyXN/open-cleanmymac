from __future__ import annotations

import os
import plistlib
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from openclean.cleanup import execute_cleanup
from openclean.engine import IgnoreRules, scan_points
from openclean.scanpoints import ScanPoint
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
