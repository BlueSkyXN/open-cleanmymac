from __future__ import annotations

import contextlib
import io
import json
import os
import plistlib
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest import mock

from openclean.cleanup import execute_cleanup, select_cleanup_items
from openclean.cli import main
from openclean.engine import IgnoreRules, scan_domains, scan_points
from openclean.processes import OpenFileSnapshot, ProcessDetectionError, ProcessSnapshot
from openclean.scanpoints import AI_TOOL_JUNK, DEVELOPER_JUNK, DOMAINS, ScanPoint
from openclean.updater import assess_updater_candidate


class CacheGapRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(os.environ, {"HOME": str(self.home)}).start()
        mock.patch("openclean.updater._application_roots",
                   return_value=(self.home / "Applications",)).start()
        self.scan_processes = mock.patch("openclean.engine.capture_process_snapshot",
                                         return_value=ProcessSnapshot(())).start()
        self.live_processes = mock.patch("openclean.cleanup.capture_process_snapshot",
                                         return_value=ProcessSnapshot(())).start()
        mock.patch("openclean.storage_diagnostics.capture_process_snapshot",
                   return_value=ProcessSnapshot(())).start()
        mock.patch("openclean.storage_diagnostics.capture_open_file_snapshot",
                   return_value=OpenFileSnapshot(())).start()

    def app(self, path: Path, bundle_id: str, version: str) -> None:
        contents = path / "Contents"
        contents.mkdir(parents=True, exist_ok=True)
        (contents / "Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": bundle_id,
            "CFBundleShortVersionString": version,
        }))

    def cache(self, relative: str) -> Path:
        path = self.home / relative
        path.mkdir(parents=True, exist_ok=True)
        (path / "entry.bin").write_bytes(b"cache" * 1024)
        return path

    def workbuddy(self, bundle_id: str, staged: str, installed: str = "5.5.3") -> Path:
        self.app(self.home / "Applications/WorkBuddy.app", bundle_id, installed)
        root = self.home / "Library/Caches" / f"{bundle_id}.BundleMigration"
        self.app(root / "extracted/build/WorkBuddy.app", bundle_id, staged)
        return root

    def scan(self, path: Path):
        return scan_points([ScanPoint("cache", (str(path),), "confirm")], workers=1)

    def test_both_workbuddy_ids_classify_pending_same_and_older_versions(self) -> None:
        for bundle_id in ("com.workbuddy.workbuddy", "com.tencent.workbuddy.mac"):
            for version, status in (("5.5.6", "pending_update"),
                                    ("5.5.3", "same_version_residue"),
                                    ("5.5.2", "older_version_residue")):
                with self.subTest(bundle_id=bundle_id, version=version):
                    root = self.workbuddy(bundle_id, version)
                    result = self.scan(root)
                    self.assertTrue(result.complete)
                    item, = result.items
                    self.assertEqual(item.updater_status, status)
                    self.assertEqual((item.installed_version, item.staged_version), ("5.5.3", version))
                    self.assertEqual(item.actionable, status != "pending_update")
                    self.assertEqual(item.safety, "critical")
                    self.assertFalse(item.preselected)
                    self.assertTrue(item.requires_explicit_selection)
                    self.assertEqual(select_cleanup_items([item], include_critical=True), [])

    def test_new_workbuddy_archive_metadata_and_partial_zip_fail_closed(self) -> None:
        bundle_id = "com.tencent.workbuddy.mac"
        self.app(self.home / "Applications/WorkBuddy.app", bundle_id, "5.5.3")
        root = self.home / "Library/Caches" / f"{bundle_id}.BundleMigration"
        archive = root / "downloads/WorkBuddy-test.zip"
        archive.parent.mkdir(parents=True)
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("WorkBuddy.app/Contents/Info.plist", plistlib.dumps({
                "CFBundleIdentifier": bundle_id, "CFBundleShortVersionString": "5.5.6",
            }))
        self.assertEqual(self.scan(root).items[0].updater_status, "pending_update")
        archive.write_bytes(b"PK incomplete download")
        item, = self.scan(root).items
        self.assertEqual(item.updater_status, "version_unknown")
        self.assertFalse(item.actionable)

    def test_missing_ambiguous_or_wrong_bundle_cannot_become_residue(self) -> None:
        bundle_id = "com.tencent.workbuddy.mac"
        root = self.workbuddy(bundle_id, "5.5.3")
        installed = self.home / "Applications/WorkBuddy.app"
        self.app(installed, "com.example.unrelated", "99.0")
        self.assertEqual(self.scan(root).items[0].updater_status, "installed_app_missing")
        self.app(installed, bundle_id, "5.5.3")
        self.app(self.home / "Applications/WorkBuddy-copy.app", bundle_id, "5.5.6")
        self.assertEqual(self.scan(root).items[0].updater_status, "version_unknown")
        self.app(root / "extracted/build/WorkBuddy.app", "com.tencent.workbuddy.mac.lookalike", "5.5.3")
        self.assertEqual(self.scan(root).items[0].updater_status, "version_unknown")
        sibling = self.cache("Library/Caches/com.tencent.workbuddy.mac.BundleMigration-backup")
        self.assertIsNone(assess_updater_candidate(sibling, home=self.home))

    def test_new_workbuddy_running_and_unknown_process_states_block(self) -> None:
        root = self.workbuddy("com.tencent.workbuddy.mac", "5.5.3")
        for state in ("running", "unknown"):
            with self.subTest(state=state):
                self.scan_processes.return_value = ProcessSnapshot(("/Applications/WorkBuddy.app",))
                self.scan_processes.side_effect = ProcessDetectionError("unavailable") if state == "unknown" else None
                item, = self.scan(root).items
                self.assertFalse(item.actionable)
                self.assertEqual(item.updater_status, "same_version_residue")

    def test_new_workbuddy_pending_update_protects_parent_and_child_scopes(self) -> None:
        root = self.workbuddy("com.tencent.workbuddy.mac", "5.5.6")
        for target in (root.parent, root / "extracted", root / "extracted/build/WorkBuddy.app"):
            with self.subTest(target=target):
                item, = self.scan(target).items
                self.assertFalse(item.actionable)
                self.assertIn("updater", item.action_block_reason)

    def test_new_workbuddy_rechecks_update_and_process_before_execution(self) -> None:
        bundle_id = "com.tencent.workbuddy.mac"
        for change in ("new_version", "started", "new_staging"):
            with self.subTest(change=change):
                root = self.workbuddy(bundle_id, "5.5.3")
                self.live_processes.return_value = ProcessSnapshot(())
                if change == "new_staging":
                    # 扫描时只有普通缓存；执行前才出现同版本暂存包。
                    root = self.cache("Library/Caches/com.tencent.workbuddy.mac.BundleMigration")
                    staged = root / "extracted/build/WorkBuddy.app/Contents/Info.plist"
                    staged.unlink()
                    (staged.parent).rmdir()
                    (staged.parent.parent).rmdir()
                item, = self.scan(root).items
                self.assertTrue(item.actionable)
                if change == "started":
                    self.live_processes.return_value = ProcessSnapshot(("/Applications/WorkBuddy.app",))
                else:
                    self.app(root / "extracted/build/WorkBuddy.app", bundle_id,
                             "5.5.6" if change == "new_version" else "5.5.3")
                report = execute_cleanup([item], IgnoreRules(), home=self.home)
                self.assertFalse(report.complete)
                self.assertEqual(report.outcomes[0].status, "blocked")
                self.assertTrue(root.exists())
                self.assertFalse((self.home / ".Trash").exists())

    def test_confirmed_new_workbuddy_residue_moves_only_the_selected_root(self) -> None:
        root = self.workbuddy("com.tencent.workbuddy.mac", "5.5.3")
        keep = self.cache("Library/Caches/unselected")
        item, = self.scan(root).items
        selected = select_cleanup_items([item], selectors=[str(root)], include_critical=True)
        trash = self.home / ".Trash"
        trash.mkdir(mode=0o700)
        report = execute_cleanup(selected, IgnoreRules(), home=self.home,
                                 trash_resolver=lambda _: trash)
        self.assertTrue(report.complete)
        self.assertFalse(root.exists())
        self.assertTrue((trash / root.name / "extracted/build/WorkBuddy.app/Contents/Info.plist").exists())
        self.assertTrue((keep / "entry.bin").exists())

    def codex_points(self):
        return [replace(p, domain="ai") for p in AI_TOOL_JUNK
                if p.category in ("Codex Electron 缓存", "Codex 浏览器 CacheStorage 保留期")]

    def scan_codex(self, ignore=None):
        with mock.patch.dict(DOMAINS, {"ai": self.codex_points()}):
            return scan_domains(["ai"], ignore=ignore, workers=1)

    def test_codex_precise_caches_and_readonly_cache_storage_in_cli(self) -> None:
        base = "Library/Application Support/Codex/"
        expected = {self.cache(base + path) for path in (
            "Cache", "component_crx_cache", "Partitions/codex-browser-app/Code Cache",
            "Default/Partitions/codex-browser-app/GPUCache",
        )}
        diagnostic = self.cache(base + "Default/Partitions/codex-browser-app/Service Worker/CacheStorage")
        protected = [self.cache(base + path) for path in (
            "Cookies", "Default/History", "Default/IndexedDB", "Local Storage", "WidevineCdm",
            "WasmTtsEngine", "Cache-backup", "Default/Partitions/other/Cache",
        )]
        rules = self.home / "rules.json"
        rules.write_text('{"schema_version": 1}', encoding="utf-8")
        output = io.StringIO()
        with mock.patch.dict(DOMAINS, {"ai": self.codex_points()}), contextlib.redirect_stdout(output):
            status = main(["clean", "ai", "--rules", str(rules), "--json"])
        self.assertEqual(status, 0)
        payload = json.loads(output.getvalue())
        items = [item for category in payload["categories"] for item in category["items"]]
        self.assertEqual({item["path"] for item in items}, {str(p) for p in expected | {diagnostic}})
        self.assertTrue(all(not item["preselected"] for item in items))
        result = self.scan_codex()
        self.assertEqual({item.path for item in result.items}, expected | {diagnostic})
        for item in result.items:
            self.assertEqual(item.actionable, item.path != diagnostic)
            if item.path == diagnostic:
                self.assertEqual(item.diagnostic_kind, "retention")
                report = execute_cleanup([item], IgnoreRules(), home=self.home)
                self.assertFalse(report.complete)
        self.assertTrue(all((path / "entry.bin").exists() for path in protected))

    def test_codex_caches_keep_running_and_execution_guards(self) -> None:
        cache = self.cache("Library/Application Support/Codex/Partitions/codex-browser-app/Cache")
        item, = self.scan_codex().items
        self.assertTrue(item.actionable)
        self.scan_processes.return_value = ProcessSnapshot(("/Applications/ChatGPT.app",))
        running, = self.scan_codex().items
        self.assertFalse(running.actionable)
        self.live_processes.return_value = self.scan_processes.return_value
        report = execute_cleanup([item], IgnoreRules(), home=self.home)
        self.assertFalse(report.complete)
        self.assertTrue(cache.exists())
        self.scan_processes.side_effect = ProcessDetectionError("unavailable")
        unknown, = self.scan_codex().items
        self.assertFalse(unknown.actionable)

    def test_codex_partition_symlinks_and_ignored_storage_are_not_scanned(self) -> None:
        external = self.cache("external/Cache")
        self.cache("external/Service Worker/CacheStorage")
        parent = self.home / "Library/Application Support/Codex/Default/Partitions"
        parent.mkdir(parents=True)
        (parent / "codex-browser-app").symlink_to(external.parent, target_is_directory=True)
        result = self.scan_codex()
        self.assertEqual(result.items, [])
        direct = self.cache("Library/Application Support/Codex/Service Worker/CacheStorage")
        result = self.scan_codex(ignore=IgnoreRules([str(direct)]))
        self.assertEqual(result.items, [])

    def test_go_and_homebrew_environment_paths_retain_selection_and_trust_boundary(self) -> None:
        for category, variable in (("Go 构建缓存", "GOCACHE"), ("Go module cache", "GOMODCACHE"),
                                   ("Homebrew 缓存", "HOMEBREW_CACHE")):
            point = next(p for p in DEVELOPER_JUNK if p.category == category)
            trusted = self.cache(f".cache/{variable}")
            outside = self.cache(f"Documents/{variable}")
            with self.subTest(variable=variable), mock.patch.dict(os.environ, {variable: str(trusted)}):
                result = scan_points([point], workers=1)
                item, = result.items
                self.assertEqual(item.path, trusted)
                self.assertEqual(item.path_source, "environment")
                self.assertEqual(item.safety, "confirm")
                self.assertTrue(item.requires_explicit_selection)
                self.assertFalse(item.preselected)
                self.assertEqual(select_cleanup_items([item], include_confirm=True), [])
            with self.subTest(outside=variable), mock.patch.dict(os.environ, {variable: str(outside)}):
                result = scan_points([point], workers=1)
                self.assertEqual(result.items, [])
                self.assertEqual([issue.code for issue in result.issues], ["unsafe_environment_path"])
                self.assertTrue((outside / "entry.bin").exists())


if __name__ == "__main__":
    unittest.main()
