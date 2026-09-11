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

from openclean.analyzer import analyze_path
from openclean.application_ownership import ApplicationResolver
from openclean.cleanup import execute_cleanup
from openclean.cleanup_guards import CleanupGuardContext
from openclean.cli import main
from openclean.engine import IgnoreRules, scan_points, scan_project_artifacts
from openclean.filesystem import lstat_retry
from openclean.knowledge_base import KnowledgeBase
from openclean.processes import ProcessDetectionError, ProcessSnapshot
from openclean.scanpoints import DEVELOPER_JUNK, DOMAINS, SYSTEM_JUNK, ScanPoint


class CleanupScopeGuardsTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.trash = self.home / ".Trash"
        self.rules = self.home / "rules.json"
        self.rules.write_text('{"schema_version": 1}', encoding="utf-8")
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(os.environ, {"HOME": str(self.home)}).start()
        mock.patch("openclean.updater._application_roots",
                   return_value=(self.home / "Applications",)).start()
        self.scan_processes = mock.patch("openclean.engine.capture_process_snapshot",
                                         return_value=ProcessSnapshot(())).start()
        self.live_processes = mock.patch("openclean.cleanup.capture_process_snapshot",
                                         return_value=ProcessSnapshot(())).start()

    def cache(self, relative: str) -> Path:
        path = self.home / relative
        path.mkdir(parents=True, exist_ok=True)
        (path / "entry.bin").write_bytes(b"cache")
        return path

    def app(self, path: Path, version: str = "1.0", bundle_id: str = "com.workbuddy.workbuddy") -> None:
        contents = path / "Contents"
        contents.mkdir(parents=True, exist_ok=True)
        (contents / "Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": bundle_id, "CFBundleShortVersionString": version,
        }))

    def scan(self, target: Path):
        return scan_points([ScanPoint("scope", (str(target),), "critical")], workers=1).items[0]

    def test_clean_and_analyze_protect_same_scope(self) -> None:
        cache = self.cache("Library/Caches/com.openai.codex")
        child = self.cache("Library/Caches/com.openai.codex/nested")
        sibling = self.cache("Library/Caches/com.openai.codex-backup")
        uv = next(p for p in DEVELOPER_JUNK if p.category == "uv 缓存")
        generic = next(p for p in SYSTEM_JUNK if p.category == "用户缓存")
        for command, state in (("Codex", "running"), ("", "stopped"), ("", "unknown")):
            self.scan_processes.return_value = ProcessSnapshot((command,) if command else ())
            self.scan_processes.side_effect = ProcessDetectionError("unavailable") if state == "unknown" else None
            for entry in ("junk", "dev", "analyze"):
                for target in (cache, child, cache.parent, sibling):
                    if entry == "junk" and target != cache:
                        continue
                    with self.subTest(entry=entry, target=target.name, state=state), mock.patch.dict(
                        os.environ, {"UV_CACHE_DIR": str(target)},
                    ), mock.patch.dict(DOMAINS, {"developer": [uv], "system": [generic]}):
                        args = (["analyze", str(target.parent)] if entry == "analyze"
                                else ["clean", entry, "--include-critical", "--include-confirm"])
                        blocked = target != sibling and state != "stopped"
                        if blocked:
                            args.append("--yes")
                        stdout = io.StringIO()
                        with contextlib.redirect_stdout(stdout):
                            status = main([*args, "--select", str(target), "--json", "--rules", str(self.rules)])
                        # Analyze 同时报告本层其它候选的检测失败，整份报告仍为 incomplete。
                        incomplete = entry == "analyze" and state == "unknown" and target == sibling
                        self.assertEqual(status, 2 if blocked else 1 if incomplete else 0)
                        payload = json.loads(stdout.getvalue())
                        if incomplete:
                            self.assertEqual(payload["selection"]["paths"], [str(sibling)])
                        self.assertTrue((cache / "entry.bin").exists())
                        self.assertFalse(self.trash.exists())

    def test_execute_rediscovers_new_protected_child_without_old_markers(self) -> None:
        for relative, command in (("Library/Caches/com.openai.codex", "Codex"),
                                  (".cache/opencode", "opencode")):
            with self.subTest(relative=relative):
                parent = self.cache(str(Path(relative).parent))
                item = self.scan(parent)
                self.assertEqual(item.running_process_markers, ())
                protected = self.cache(relative)
                self.live_processes.return_value = ProcessSnapshot((command,))
                ordinary = self.cache("ordinary")
                report = execute_cleanup([self.scan(ordinary), item], IgnoreRules(), home=self.home)
                self.assertEqual([o.status for o in report.outcomes], ["not_run", "blocked"])
                self.assertTrue((protected / "entry.bin").exists())
                self.assertTrue(ordinary.exists())
                self.assertFalse(self.trash.exists())

    def test_new_child_after_batch_preflight_is_checked_before_trash_creation(self) -> None:
        parent = self.cache("Library/Caches")
        ordinary = self.cache("ordinary")
        selected = [self.scan(ordinary), self.scan(parent)]

        def prepare_trash(_):
            self.cache("Library/Caches/com.openai.codex")
            self.live_processes.return_value = ProcessSnapshot(("Codex",))
            self.trash.mkdir(mode=0o700, exist_ok=True)
            return self.trash

        report = execute_cleanup(selected, IgnoreRules(), home=self.home, trash_resolver=prepare_trash)
        self.assertEqual([o.status for o in report.outcomes], ["moved_to_trash", "failed"])
        self.assertTrue((parent / "com.openai.codex/entry.bin").exists())
        self.assertEqual([p.name for p in self.trash.iterdir()], ["ordinary"])

    def test_updater_parent_and_child_require_exact_root_review(self) -> None:
        cache = self.cache("Library/Caches/com.workbuddy.workbuddy.BundleMigration")
        self.app(self.home / "Applications/WorkBuddy.app")
        for version in ("2.0", "1.0", "0.9"):
            self.app(cache / "extracted/build/WorkBuddy.app", version)
            for target in (cache, cache.parent, cache / "extracted"):
                with self.subTest(version=version, target=target.name):
                    item = self.scan(target)
                    self.assertEqual(item.actionable, target == cache and version != "2.0")
                    self.assertEqual(item.safety, "critical")
                    self.assertTrue(item.requires_explicit_selection)
                    if target != cache:
                        report = execute_cleanup([item], IgnoreRules(), home=self.home)
                        self.assertEqual(report.outcomes[0].status, "blocked")
                        self.assertTrue(cache.exists())
                        self.assertFalse(self.trash.exists())

    def test_updater_staging_added_after_scan_invalidates_old_selection(self) -> None:
        cache = self.cache("Library/Caches/com.workbuddy.workbuddy.BundleMigration")
        self.app(self.home / "Applications/WorkBuddy.app")
        selected = [self.scan(cache), self.scan(cache.parent)]
        for item in selected:
            self.assertEqual(item.updater_status, "")
        for version in ("1.0", "2.0"):
            self.app(cache / "extracted/build/WorkBuddy.app", version)
            for item in selected:
                with self.subTest(version=version, path=item.path):
                    report = execute_cleanup([item], IgnoreRules(), home=self.home)
                    self.assertEqual(report.outcomes[0].status, "blocked")
                    self.assertTrue(cache.exists())
                    self.assertFalse(self.trash.exists())

    def test_new_updater_staging_during_trash_setup_blocks_final_move(self) -> None:
        cache = self.cache("Library/Caches/com.workbuddy.workbuddy.BundleMigration")
        self.app(self.home / "Applications/WorkBuddy.app")
        item = self.scan(cache)

        def prepare_trash(_):
            self.app(cache / "extracted/build/WorkBuddy.app", "2.0")
            self.trash.mkdir(mode=0o700)
            return self.trash

        report = execute_cleanup([item], IgnoreRules(), home=self.home, trash_resolver=prepare_trash)
        self.assertEqual(report.outcomes[0].status, "failed")
        self.assertTrue(cache.exists())
        self.assertEqual(list(self.trash.iterdir()), [])

    def test_unchanged_stopped_cache_and_exact_updater_still_move(self) -> None:
        for version in (None, "1.0", "0.9"):
            with self.subTest(version=version):
                cache = self.cache("Library/Caches/com.workbuddy.workbuddy.BundleMigration")
                self.app(self.home / "Applications/WorkBuddy.app")
                if version:
                    self.app(cache / "extracted/build/WorkBuddy.app", version)
                report = execute_cleanup([self.scan(cache)], IgnoreRules(), home=self.home)
                self.assertEqual(report.outcomes[0].status, "moved_to_trash")
                self.assertFalse(cache.exists())

    def test_dynamic_bundle_owner_covers_parent_and_child(self) -> None:
        cache = self.cache("Library/Caches/org.example.editor")
        child = self.cache("Library/Caches/org.example.editor/nested")
        app = self.home / "Applications/Editor.app"
        self.app(app, bundle_id="org.example.editor")
        resolver = ApplicationResolver(home=self.home, application_roots=(app.parent,),
                                       runner=lambda *a, **kw: subprocess.CompletedProcess([], 0, "", ""))
        self.scan_processes.return_value = ProcessSnapshot((str(app / "Contents/MacOS/Editor"),))
        with mock.patch("openclean.engine.ApplicationResolver", return_value=resolver):
            for target in (cache, child, cache.parent):
                with self.subTest(target=target.name):
                    self.assertFalse(self.scan(target).actionable)

    def test_purge_within_running_application_cache_is_report_only(self) -> None:
        project = self.cache(".cache/opencode/project")
        (project / "package.json").write_text("{}", encoding="utf-8")
        self.cache(".cache/opencode/project/node_modules")
        self.scan_processes.return_value = ProcessSnapshot(("opencode",))
        result = scan_project_artifacts([project])
        self.assertEqual(len(result.items), 1)
        self.assertFalse(result.items[0].actionable)

    def test_analyze_execution_ignores_no_known_runtime_guard(self) -> None:
        cache = self.cache("Library/Caches/com.openai.codex")
        result = analyze_path(cache.parent)
        item = next(entry.item for entry in result.entries if entry.item.path == cache)
        self.live_processes.side_effect = ProcessDetectionError("unavailable")
        report = execute_cleanup([item], IgnoreRules(), home=self.home)
        self.assertEqual(report.outcomes[0].status, "blocked")
        self.assertTrue(cache.exists())
        self.assertFalse(self.trash.exists())

    def test_updater_probe_never_traverses_unsafe_staging_ancestors(self) -> None:
        cache = self.cache("Library/Caches/com.workbuddy.workbuddy.BundleMigration")
        target = self.cache("Library/Caches/com.workbuddy.workbuddy.BundleMigration/ordinary")
        staged = cache / "extracted/build/WorkBuddy.app"
        self.app(staged, "2.0")
        staging_root = cache / "extracted"
        for kind in ("cloud", "ignored", "symlink"):
            with self.subTest(kind=kind), contextlib.ExitStack() as stack:
                protection = IgnoreRules()
                if kind == "cloud":
                    stack.enter_context(mock.patch("openclean.models.FileFacts.is_dataless",
                                                   new=property(lambda facts: facts.path == staging_root)))
                elif kind == "ignored":
                    protection = IgnoreRules(knowledge_base=KnowledgeBase.from_mapping({
                        "schema_version": 1, "protect": {"paths": [str(staging_root)]},
                    }))
                else:
                    outside = self.home / "outside-staging"
                    staging_root.rename(outside)
                    staging_root.symlink_to(outside, target_is_directory=True)
                probe = stack.enter_context(mock.patch("openclean.cleanup_guards.lstat_retry", wraps=lstat_retry))
                assessment = CleanupGuardContext(protection, lambda: ProcessSnapshot(())).assess(target)
                self.assertTrue(assessment.block_reason)
                self.assertEqual(assessment.updater.status, "version_unknown")
                self.assertFalse(any(Path(call.args[0]) != staging_root
                                     and Path(call.args[0]).is_relative_to(staging_root)
                                     for call in probe.call_args_list))
                if kind == "ignored":
                    self.assertFalse(any(call.args[0] == staging_root for call in probe.call_args_list))

    def test_new_dynamic_owner_at_execution_is_not_hidden_by_empty_scan_markers(self) -> None:
        cache = self.cache("Library/Caches/org.example.editor")
        resolver = ApplicationResolver(home=self.home, application_roots=(self.home / "Applications",),
                                       runner=lambda *a, **kw: subprocess.CompletedProcess([], 0, "", ""))
        with mock.patch("openclean.engine.ApplicationResolver", return_value=resolver):
            item = self.scan(cache)
        self.assertEqual(item.running_process_markers, ())
        app = self.home / "Applications/Editor.app"
        self.app(app, bundle_id="org.example.editor")
        self.live_processes.return_value = ProcessSnapshot((str(app / "Contents/MacOS/Editor"),))
        resolver = ApplicationResolver(home=self.home, application_roots=(app.parent,),
                                       runner=lambda *a, **kw: subprocess.CompletedProcess([], 0, "", ""))
        with mock.patch("openclean.cleanup_guards.ApplicationResolver", return_value=resolver):
            report = execute_cleanup([item], IgnoreRules(), home=self.home)
        self.assertEqual(report.outcomes[0].status, "blocked")
        self.assertTrue(cache.exists())
        self.assertFalse(self.trash.exists())
