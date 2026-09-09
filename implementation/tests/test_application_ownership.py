from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openclean.application_ownership import ApplicationResolver, process_markers_for_path
from openclean.cleanup import execute_cleanup
from openclean.engine import IgnoreRules, scan_domains, scan_points
from openclean.processes import ProcessSnapshot
from openclean.scanpoints import SYSTEM_JUNK


class ApplicationResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        self.apps = self.home / "Applications"
        self.apps.mkdir()
        self.bundle_id = "org.example.editor"
        self.candidate = self.home / "Library/Caches" / self.bundle_id
        self.runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))

    def app(self, name: str = "Editor.app", bundle_id: str | None = None) -> Path:
        app = self.apps / name
        contents = app / "Contents"
        contents.mkdir(parents=True)
        (contents / "Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": bundle_id or self.bundle_id,
        }))
        return app

    def resolver(self, **kwargs) -> ApplicationResolver:
        return ApplicationResolver(
            home=self.home, application_roots=(self.apps,), runner=self.runner, **kwargs
        )

    def test_fallback_reads_bundle_without_requiring_updater_version(self) -> None:
        app = self.app()
        resolver = self.resolver()
        result = resolver.resolve(self.candidate)
        self.assertEqual(result.status, "resolved")
        self.assertEqual(result.process_markers, (str(app / "Contents") + "/",))
        self.assertEqual(resolver.resolve(self.candidate), result)
        self.runner.assert_called_once()
        self.assertEqual(self.runner.call_args.args[0], [
            "/usr/bin/mdfind", "-0", 'kMDItemCFBundleIdentifier == "org.example.editor"',
        ])

    def test_spotlight_result_is_verified_and_can_find_nonstandard_install(self) -> None:
        external = self.home / "external"
        external.mkdir()
        app = self.app().rename(external / "Editor.app")
        self.runner.return_value.stdout = str(app) + "\0"
        self.assertEqual(self.resolver().resolve(self.candidate).status, "resolved")
        self.runner.return_value.stdout = str(app) + "\0"
        wrong = self.candidate.with_name("org.example.other")
        self.assertEqual(self.resolver().resolve(wrong).status, "unavailable")

    def test_multiple_installs_protect_all_verified_paths(self) -> None:
        first, second = self.app(), self.app("Editor Beta.app")
        result = self.resolver().resolve(self.candidate)
        self.assertEqual(result.status, "multiple")
        snapshot = ProcessSnapshot((str(second / "Contents/Frameworks/Helper.app/Contents/MacOS/Helper"),))
        self.assertTrue(snapshot.any_running(result.process_markers))
        self.assertEqual(len(result.process_markers), 2)
        self.assertFalse(ProcessSnapshot((str(first) + "-backup/Contents/MacOS/Editor",)).any_running(result.process_markers))

    def test_failure_is_not_absence_and_fallback_still_works(self) -> None:
        self.runner.side_effect = subprocess.TimeoutExpired("mdfind", 0.5)
        self.assertEqual(self.resolver().resolve(self.candidate).status, "unavailable")
        self.app()
        self.assertEqual(self.resolver().resolve(self.candidate).status, "resolved")

    def test_nonzero_and_oversized_spotlight_results_are_unavailable(self) -> None:
        self.runner.return_value = subprocess.CompletedProcess([], 1, "", "failed")
        self.assertEqual(self.resolver().resolve(self.candidate).status, "unavailable")
        self.runner.return_value = subprocess.CompletedProcess([], 0, "x" * 65537, "")
        self.assertEqual(self.resolver().resolve(self.candidate).status, "unavailable")

    def test_oversized_plist_is_not_parsed(self) -> None:
        app = self.app()
        (app / "Contents/Info.plist").write_bytes(b"x" * 131073)
        with mock.patch("openclean.application_ownership.plistlib.loads") as loads:
            self.assertEqual(self.resolver().resolve(self.candidate).status, "unavailable")
        loads.assert_not_called()

    def test_deep_binary_plist_does_not_abort_normal_cache_scan(self) -> None:
        invalid_app = self.app("Unrelated.app", "org.example.unrelated")
        valid_app = self.app()
        nested = "value"
        for _ in range(700):
            nested = [nested]
        original_limit = sys.getrecursionlimit()
        try:
            # 生成大小合法但超出解析递归深度的真实 binary plist，再恢复解析上限。
            sys.setrecursionlimit(10000)
            payload = plistlib.dumps({"CFBundleIdentifier": "org.example.unrelated",
                                       "nested": nested}, fmt=plistlib.FMT_BINARY)
            sys.setrecursionlimit(1000)
            self.assertLess(len(payload), 131072)
            (invalid_app / "Contents/Info.plist").write_bytes(payload)
            self.candidate.mkdir(parents=True)
            (self.candidate / "cache.bin").write_bytes(b"cache" * 1024)
            point = next(p for p in SYSTEM_JUNK if p.category == "用户缓存")
            for spotlight_output in ("", str(invalid_app) + "\0"):
                with self.subTest(spotlight_hit=bool(spotlight_output)):
                    self.runner.return_value.stdout = spotlight_output
                    resolver = self.resolver()
                    with mock.patch.dict(os.environ, {"HOME": str(self.home)}), mock.patch(
                        "openclean.engine.ApplicationResolver", return_value=resolver
                    ), mock.patch("openclean.engine.capture_process_snapshot", return_value=ProcessSnapshot(
                        (str(valid_app / "Contents/MacOS/Editor"),)
                    )):
                        result = scan_points([point], workers=1)
                    self.assertTrue(result.complete, [(i.code, i.message) for i in result.issues])
                    self.assertEqual([item.path for item in result.items], [self.candidate])
                    self.assertFalse(result.items[0].actionable)
                    self.assertIn(str(valid_app / "Contents") + "/", result.items[0].running_process_markers)
                    unknown = resolver.resolve(self.candidate.with_name("org.example.unrelated"))
                    self.assertEqual(unknown.status, "unavailable")
                    self.assertEqual(unknown.process_markers, ())
        finally:
            sys.setrecursionlimit(original_limit)

    def test_placeholder_metadata_is_not_read(self) -> None:
        self.app()
        with mock.patch("openclean.application_ownership.FileFacts.is_probable_cloud_placeholder",
                        new_callable=mock.PropertyMock, return_value=True), mock.patch(
            "openclean.application_ownership.plistlib.loads"
        ) as loads:
            self.assertEqual(self.resolver().resolve(self.candidate).status, "unavailable")
        loads.assert_not_called()

    def test_not_found_and_query_budget_are_distinct(self) -> None:
        resolver = self.resolver(max_queries=1)
        self.assertEqual(resolver.resolve(self.candidate).status, "not_found")
        self.assertEqual(resolver.resolve(self.candidate.with_name("org.example.second")).status, "limit_reached")
        self.runner.assert_called_once()
        self.assertEqual(self.resolver(time_budget=0).resolve(self.candidate).status, "limit_reached")

    def test_scope_and_query_syntax_are_not_inferred(self) -> None:
        resolver = self.resolver()
        for path in (
            self.home / "arbitrary" / self.bundle_id,
            self.candidate / "nested",
            self.candidate.with_name('org.example.*'),
            self.candidate.with_name('org.example."bad'),
            self.candidate.with_name("Editor"),
        ):
            self.assertEqual(resolver.resolve(path).status, "not_applicable")
        self.runner.assert_not_called()
        self.app()
        self.assertEqual(resolver.resolve(self.candidate.with_name(self.bundle_id + ".helper")).status, "not_found")

    def test_only_explicit_darwin_root_is_allowed(self) -> None:
        self.app()
        root = self.home / "darwin/C"
        resolver = self.resolver()
        self.assertEqual(resolver.resolve(root / self.bundle_id).status, "not_applicable")
        self.assertEqual(resolver.resolve(root / self.bundle_id, darwin_cache_root=root).status, "resolved")

    def test_symlink_and_malformed_metadata_do_not_prove_ownership(self) -> None:
        app = self.app()
        info = app / "Contents/Info.plist"
        original = info.read_bytes()
        info.write_bytes(b"not a plist")
        self.assertEqual(self.resolver().resolve(self.candidate).status, "unavailable")
        info.unlink()
        target = self.home / "metadata.plist"
        target.write_bytes(original)
        info.symlink_to(target)
        self.assertEqual(self.resolver().resolve(self.candidate).status, "unavailable")
        info.unlink()
        info.write_bytes(original)
        moved = app.rename(self.home / "Real.app")
        app.symlink_to(moved, target_is_directory=True)
        self.runner.return_value.stdout = str(app) + "\0"
        self.assertEqual(self.resolver().resolve(self.candidate).status, "unavailable")

    def scan(self, command: str = "", *, domains: bool = False):
        self.candidate.mkdir(parents=True, exist_ok=True)
        (self.candidate / "cache.bin").write_bytes(b"cache" * 1024)
        point = next(point for point in SYSTEM_JUNK if point.category == "用户缓存")
        resolver = self.resolver()
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}), mock.patch(
            "openclean.engine.ApplicationResolver", return_value=resolver
        ), mock.patch("openclean.engine.capture_process_snapshot", return_value=ProcessSnapshot((command,))), mock.patch.dict(
            "openclean.engine.DOMAINS", {"system": [point]}
        ):
            result = scan_domains(["system"], workers=1) if domains else scan_points([point], workers=1)
            return next(item for item in result.items
                        if item.path == self.candidate)

    def test_domain_task_graph_passes_the_shared_resolver(self) -> None:
        app = self.app()
        item = self.scan(str(app / "Contents/MacOS/Editor"), domains=True)
        self.assertFalse(item.actionable)
        self.assertEqual(item.domain, "system")
        self.assertIn("元数据", item.note)

    def test_running_dynamic_owner_is_blocked_and_unknown_behavior_is_preserved(self) -> None:
        app = self.app()
        item = self.scan(str(app / "Contents/MacOS/Editor"))
        self.assertFalse(item.actionable)
        self.assertFalse(item.preselected)
        self.assertIn("元数据", item.note)
        self.candidate = self.candidate.with_name("org.example.unknown")
        unknown = self.scan()
        self.assertTrue(unknown.actionable)
        self.assertFalse(unknown.preselected)
        self.assertIn("不表示应用已卸载", unknown.note)

    def test_static_rules_do_not_depend_on_spotlight(self) -> None:
        self.candidate = self.candidate.with_name("com.openai.codex")
        item = self.scan("/Applications/ChatGPT.app/Contents/MacOS/ChatGPT")
        self.assertFalse(item.actionable)
        self.runner.assert_not_called()
        self.assertEqual(item.running_process_markers, process_markers_for_path(self.candidate, home=self.home))

    def test_execution_checks_current_processes_and_rejects_late_start(self) -> None:
        app = self.app()
        item = self.scan()
        self.assertTrue(item.actionable)
        command = str(app / "Contents/MacOS/Editor")
        process_runner = mock.Mock(side_effect=[
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, command, ""),
        ])
        trash = mock.Mock(return_value=self.home / ".Trash")
        report = execute_cleanup([item], IgnoreRules(), home=self.home,
                                 process_runner=process_runner, trash_resolver=trash)
        self.assertFalse(report.complete)
        self.assertTrue(self.candidate.exists())
        trash.assert_not_called()


if __name__ == "__main__":
    unittest.main()
