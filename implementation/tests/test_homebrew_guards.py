from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from openclean.analyzer import analyze_path
from openclean.cleanup import execute_cleanup
from openclean.cleanup_guards import CleanupGuardContext
from openclean.cli import main
from openclean.engine import Cancelled, Control, IgnoreRules, scan_points
from openclean.homebrew import cache_block_reason, running
from openclean.knowledge_base import KnowledgeBase
from openclean.macos import filesystem_case_sensitive
from openclean.models import FileFacts, FileIdentity
from openclean.processes import ProcessDetectionError, ProcessSnapshot
from openclean.scanpoints import DEVELOPER_JUNK, SYSTEM_JUNK, ScanPoint


class HomebrewGuardsTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.cache = self.home / "Library/Caches/Homebrew"
        self.trash = self.home / ".Trash"
        self.write(self.cache / "downloads/bottle.tar.gz")
        self.brew = next(p for p in DEVELOPER_JUNK if p.category == "Homebrew 缓存")
        self.generic = next(p for p in SYSTEM_JUNK if p.category == "用户缓存")
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(os.environ, {"HOME": str(self.home)}, clear=True).start()
        self.scanner = mock.patch("openclean.engine.capture_process_snapshot", return_value=ProcessSnapshot(())).start()
        self.live = mock.patch("openclean.cleanup.capture_process_snapshot", return_value=ProcessSnapshot(())).start()

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic download\n" * 512)

    def item(self, entry: str, cache: Path | None = None):
        cache = cache or self.cache
        if entry == "dev":
            return next(i for i in scan_points([self.brew], workers=1).items if i.path == cache)
        if entry == "generic":
            return next(i for i in scan_points([self.generic], workers=1).items if i.path == cache)
        target = cache.parent if entry == "parent" else cache if entry == "analyze" else cache / "downloads"
        return next(e.item for e in analyze_path(target.parent).entries if e.item.path == target)

    def test_running_and_unknown_states_block_all_entry_points(self) -> None:
        for state in ("fetch", "install", "upgrade", "unknown"):
            self.scanner.return_value = ProcessSnapshot((f"/opt/homebrew/Library/Homebrew/vendor/ruby /opt/homebrew/Library/Homebrew/brew.rb {state} fixture",))
            self.scanner.side_effect = ProcessDetectionError("unavailable") if state == "unknown" else None
            for entry in ("dev", "generic", "analyze", "parent", "child"):
                with self.subTest(state=state, entry=entry):
                    item = self.item(entry)
                    self.assertFalse(item.actionable)
                    self.assertFalse(item.preselected)
                    self.assertIn("brew", item.running_process_markers)
                    report = execute_cleanup([item], IgnoreRules(), home=self.home)
                    self.assertEqual(report.outcomes[0].status, "blocked")
                    self.assertFalse(self.trash.exists())

    def test_process_matching_does_not_treat_arbitrary_ruby_curl_or_homebrew_path_as_brew(self) -> None:
        for command in ("/opt/homebrew/bin/python server.py", "/usr/bin/curl https://example.invalid",
                        "/usr/bin/ruby app.rb", "/usr/bin/vim homebrew.txt", "brew-helper task"):
            self.assertFalse(running(ProcessSnapshot((command,))), command)
        for command in ("brew fetch fixture", "/opt/homebrew/bin/brew upgrade",
                        "/usr/bin/ruby /usr/local/Homebrew/Library/Homebrew/brew.rb install fixture"):
            self.assertTrue(running(ProcessSnapshot((command,))), command)
        self.scanner.return_value = ProcessSnapshot(("/opt/homebrew/bin/python server.py",))
        self.assertTrue(self.item("dev").actionable)

    def test_brew_mentioned_in_nonexecution_arguments_does_not_block_any_entry(self) -> None:
        commands = (
            "/usr/bin/man brew",
            "/usr/bin/vim /opt/homebrew/bin/brew",
            "/usr/bin/git clone https://github.com/Homebrew/brew",
            "/bin/sh -c 'man brew'",
            "/bin/bash -lc 'vim /opt/homebrew/bin/brew'",
            "/usr/bin/env /usr/bin/man brew",
            "/usr/bin/ruby -e 'puts ARGV' /opt/homebrew/bin/brew",
            "/usr/bin/ruby -I brew app.rb",
            "/usr/bin/ruby -r brew app.rb",
            "/bin/bash --rcfile brew app.sh",
            "/bin/sh -s brew",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertFalse(running(ProcessSnapshot((command,))))
                self.scanner.return_value = ProcessSnapshot((command,))
                for entry in ("dev", "generic", "analyze", "parent", "child"):
                    self.assertTrue(self.item(entry).actionable)
        self.live.return_value = ProcessSnapshot(commands)
        report = execute_cleanup([self.item("dev")], IgnoreRules(), home=self.home)
        self.assertEqual(report.outcomes[0].status, "moved_to_trash")

    def test_brew_executable_and_interpreter_script_positions_remain_protected(self) -> None:
        commands = (
            "/opt/homebrew/bin/brew fetch fixture",
            "/bin/bash /opt/homebrew/bin/brew install fixture",
            "/bin/bash -- /usr/local/bin/brew upgrade fixture",
            "/bin/sh -c '/opt/homebrew/bin/brew fetch fixture'",
            "/bin/bash -lc 'exec /opt/homebrew/bin/brew fetch fixture'",
            "/usr/bin/ruby -W1 /opt/homebrew/Library/Homebrew/brew.rb fetch fixture",
            "/usr/bin/ruby --disable=gems --disable=rubyopt /opt/homebrew/Library/Homebrew/brew.rb install fixture",
            "/usr/bin/ruby -I /opt/homebrew/lib -r rubygems /opt/homebrew/Library/Homebrew/brew.rb upgrade fixture",
            "/usr/bin/ruby -- /opt/homebrew/Library/Homebrew/brew.rb fetch fixture",
            "/usr/bin/env HOMEBREW_NO_AUTO_UPDATE=1 /opt/homebrew/bin/brew fetch fixture",
            "/usr/bin/env -u RUBYOPT ruby -W1 /opt/homebrew/Library/Homebrew/brew.rb install fixture",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertTrue(running(ProcessSnapshot((command,))))
                self.scanner.return_value = ProcessSnapshot((command,))
                self.assertFalse(self.item("dev").actionable)
        self.scanner.return_value = ProcessSnapshot(())
        item = self.item("dev")
        self.live.return_value = ProcessSnapshot((commands[5],))
        report = execute_cleanup([item], IgnoreRules(), home=self.home)
        self.assertEqual(report.outcomes[0].status, "blocked")
        self.assertTrue(self.cache.exists())

    def test_brew_shell_auto_update_protects_ordinary_api_files_across_entries(self) -> None:
        self.write(self.cache / "api/formula.jws.json")
        command = "/bin/bash -p /opt/homebrew/Library/Homebrew/brew.sh install wget"
        self.assertTrue(running(ProcessSnapshot((command,))))
        self.assertEqual(cache_block_reason(self.cache, IgnoreRules()), "")
        self.scanner.return_value = ProcessSnapshot((command,))
        for entry in ("dev", "generic", "analyze", "parent", "child"):
            with self.subTest(entry=entry):
                item = self.item(entry)
                self.assertFalse(item.actionable)
                self.assertIn("brew.sh", item.running_process_markers)
        self.scanner.return_value = ProcessSnapshot(())
        item = self.item("dev")
        self.live.return_value = ProcessSnapshot((command,))
        report = execute_cleanup([item], IgnoreRules(), home=self.home)
        self.assertEqual(report.outcomes[0].status, "blocked")
        self.assertTrue((self.cache / "api/formula.jws.json").exists())
        self.assertFalse(self.trash.exists())

    def test_brew_shell_started_after_preflight_blocks_final_move(self) -> None:
        self.write(self.cache / "api/internal/packages.fixture.jws.json")
        item = self.item("dev")
        def prepare(_):
            self.live.return_value = ProcessSnapshot((
                "/bin/bash -p /opt/homebrew/Library/Homebrew/brew.sh upgrade fixture",
            ))
            self.trash.mkdir(mode=0o700)
            return self.trash
        report = execute_cleanup([item], IgnoreRules(), home=self.home, trash_resolver=prepare)
        self.assertEqual(report.outcomes[0].status, "failed")
        self.assertTrue((self.cache / "api/internal/packages.fixture.jws.json").exists())
        self.assertEqual(list(self.trash.iterdir()), [])

    def test_brew_shell_mentions_remain_unrelated(self) -> None:
        for command in (
            "/usr/bin/man brew.sh",
            "/usr/bin/vim /opt/homebrew/Library/Homebrew/brew.sh",
            "/bin/bash --rcfile /opt/homebrew/Library/Homebrew/brew.sh other.sh",
            "/bin/sh -c 'cat /opt/homebrew/Library/Homebrew/brew.sh'",
            "/usr/bin/ruby -e 'puts ARGV' /opt/homebrew/Library/Homebrew/brew.sh",
        ):
            with self.subTest(command=command):
                self.assertFalse(running(ProcessSnapshot((command,))))
                self.scanner.return_value = ProcessSnapshot((command,))
                self.assertTrue(self.item("dev").actionable)

    def test_case_aliases_keep_process_and_incomplete_protection(self) -> None:
        if filesystem_case_sensitive(self.home):
            self.skipTest("真实大小写别名需不敏感文件系统")
        alias = self.home / "library/caches/homebrew"
        self.assertEqual(alias.stat().st_ino, self.cache.stat().st_ino)
        for state in ("running", "unknown", "incomplete"):
            self.scanner.return_value = ProcessSnapshot((
                "/bin/bash -p /opt/homebrew/Library/Homebrew/brew.sh install wget",
            ) if state == "running" else ())
            self.scanner.side_effect = ProcessDetectionError("unknown") if state == "unknown" else None
            if state == "incomplete":
                self.write(self.cache / "downloads/fixture.incomplete")
            for target in (alias, alias / "downloads", alias.parent, alias.parent.parent):
                with self.subTest(state=state, target=target.name):
                    item, = scan_points([ScanPoint("alias", (str(target),))], workers=1).items
                    self.assertFalse(item.actionable)
                    self.assertIn("brew.sh", item.running_process_markers)
            analysis = analyze_path(alias)
            self.assertTrue(analysis.entries)
            self.assertTrue(all(not entry.item.actionable for entry in analysis.entries))
        self.scanner.side_effect = None
        self.scanner.return_value = ProcessSnapshot(())
        (self.cache / "downloads/fixture.incomplete").unlink()
        self.assertTrue(all(entry.item.actionable for entry in analyze_path(alias).entries))

    def test_case_alias_execution_rechecks_activity_without_old_markers(self) -> None:
        if filesystem_case_sensitive(self.home):
            self.skipTest("真实大小写别名需不敏感文件系统")
        alias = self.cache.with_name("homebrew")
        item, = scan_points([ScanPoint("alias", (str(alias),))], workers=1).items
        self.assertTrue(item.actionable)
        item = replace(item, running_process_markers=())
        self.live.return_value = ProcessSnapshot(("/bin/bash -p /opt/homebrew/Library/Homebrew/brew.sh update",))
        report = execute_cleanup([item], IgnoreRules(), home=self.home)
        self.assertEqual(report.outcomes[0].status, "blocked")
        self.live.return_value = ProcessSnapshot(())
        self.write(self.cache / "downloads/new.incomplete")
        report = execute_cleanup([item], IgnoreRules(), home=self.home)
        self.assertEqual(report.outcomes[0].status, "blocked")
        self.assertTrue(self.cache.exists())
        self.assertFalse(self.trash.exists())

    def test_case_alias_environment_and_lock_descendants_are_protected(self) -> None:
        if filesystem_case_sensitive(self.home):
            self.skipTest("真实大小写别名需不敏感文件系统")
        custom = self.home / "Library/Caches/CustomBrew"
        self.write(custom / "downloads/file.incomplete")
        with mock.patch.dict(os.environ, {"HOMEBREW_CACHE": str(self.home / "library/caches/custombrew")}):
            item, = scan_points([ScanPoint("custom", (str(custom),))], workers=1).items
            self.assertFalse(item.actionable)
            self.assertIn("brew.sh", item.running_process_markers)
        protected = self.cache / "locks/nested/payload"
        self.write(protected)
        alias = self.cache.with_name("homebrew") / "LOCKS/nested/payload"
        item, = scan_points([ScanPoint("alias", (str(alias),))], workers=1).items
        self.assertFalse(item.actionable)
        self.assertIn("在途下载或锁内部", item.action_block_reason)
        prefix = self.home / "BrewPrefix"
        self.write(prefix / "var/homebrew/locks/nested/file")
        with mock.patch.dict(os.environ, {"HOMEBREW_PREFIX": str(prefix)}):
            target = self.home / "brewprefix/var/HOMEBREW/LOCKS/nested"
            item, = scan_points([ScanPoint("locks", (str(target),))], workers=1).items
            self.assertFalse(item.actionable)
            self.assertIn("Homebrew 锁目录", item.action_block_reason)

    def test_case_sensitive_or_different_identity_paths_are_not_merged(self) -> None:
        if filesystem_case_sensitive(self.home):
            self.write(self.cache.with_name("homebrew") / "downloads/other")
        alias = self.cache.with_name("homebrew")
        context = CleanupGuardContext(IgnoreRules(), lambda: ProcessSnapshot(("brew install fixture",)), home=self.home)
        with mock.patch("openclean.macos.filesystem_case_sensitive", return_value=True):
            self.assertIsNone(context._homebrew_scope(alias, self.cache))
        original = context._probe
        def different(path):
            facts = original(path)
            if facts is not None and path == alias:
                return mock.Mock(stat=facts.stat, identity=FileIdentity(99, 88, 77))
            return facts
        with mock.patch("openclean.macos.filesystem_case_sensitive", return_value=False), \
                mock.patch.object(context, "_probe", side_effect=different):
            self.assertIsNone(context._homebrew_scope(alias, self.cache))
        self.assertIsNone(context._homebrew_scope(self.cache.with_name("Homebrew-backup"), self.cache))

    def test_case_scope_unknown_or_unsafe_ancestors_do_not_trigger_protected_reads(self) -> None:
        if filesystem_case_sensitive(self.home):
            self.skipTest("真实大小写别名需不敏感文件系统")
        alias = self.cache.with_name("homebrew")
        context = CleanupGuardContext(IgnoreRules(), lambda: ProcessSnapshot(()), home=self.home)
        with mock.patch("openclean.macos.filesystem_case_sensitive", side_effect=OSError("unknown")):
            assessment = context.assess(alias)
        self.assertTrue(assessment.block_reason)
        self.assertTrue(assessment.inspection_error)
        for kind in ("protected", "cloud", "symlink"):
            with self.subTest(kind=kind), contextlib.ExitStack() as stack:
                protection = IgnoreRules()
                if kind == "protected":
                    knowledge = KnowledgeBase.from_mapping({"schema_version": 1, "protect": {"paths": [str(self.cache)]}})
                    protection = IgnoreRules(knowledge_base=knowledge)
                elif kind == "cloud":
                    stack.enter_context(mock.patch.object(FileFacts, "is_probable_cloud_placeholder",
                                                          new=property(lambda facts: facts.path == alias)))
                else:
                    moved = self.cache.with_name("elsewhere")
                    self.cache.rename(moved)
                    self.cache.symlink_to(moved, target_is_directory=True)
                context = CleanupGuardContext(protection, lambda: ProcessSnapshot(()), home=self.home)
                reader = stack.enter_context(mock.patch("openclean.homebrew.cache_block_reason"))
                assessment = context.assess(alias)
                self.assertTrue(assessment.block_reason)
                reader.assert_not_called()

    def test_partial_download_or_lock_blocks_even_when_brew_has_exited(self) -> None:
        for relative in ("downloads/sample.incomplete", "downloads/sample.lock", "locks/sample"):
            with self.subTest(relative=relative):
                marker = self.cache / relative
                self.write(marker)
                for entry in ("dev", "generic", "analyze", "parent"):
                    item = self.item(entry)
                    self.assertFalse(item.actionable)
                    self.assertIn("在途下载或锁", item.action_block_reason)
                marker.unlink()
                if marker.parent.name == "locks":
                    marker.parent.rmdir()
        self.assertTrue(self.item("dev").actionable)

    def test_descendant_inside_cache_lock_directory_cannot_bypass_marker(self) -> None:
        protected = self.cache / "locks/nested/payload"
        self.write(protected)
        item, = scan_points([ScanPoint("child", (str(protected),))], workers=1).items
        self.assertFalse(item.actionable)
        self.assertIn("在途下载或锁内部", item.action_block_reason)

    def test_scan_failure_is_incomplete_and_cancel_propagates(self) -> None:
        with mock.patch("openclean.homebrew.PathInspection.tree", side_effect=PermissionError("denied")):
            result = scan_points([self.brew], workers=1)
        self.assertFalse(result.complete)
        self.assertFalse(result.items[0].actionable)
        self.assertIn("cleanup_guard_check_failed", {i.code for i in result.issues})
        control = Control()
        control.cancel()
        with self.assertRaises(Cancelled):
            cache_block_reason(self.cache, IgnoreRules(), control.checkpoint)

    def test_cancelled_cache_check_never_publishes_unchecked_candidate(self) -> None:
        with mock.patch("openclean.homebrew.cache_block_reason", side_effect=Cancelled()):
            result = scan_points([self.brew], workers=1)
        self.assertTrue(result.cancelled)
        self.assertEqual(result.items, [])

    def test_activity_after_scan_blocks_entire_batch(self) -> None:
        item = self.item("dev")
        ordinary = self.home / "ordinary"
        self.write(ordinary / "cache.bin")
        other, = scan_points([ScanPoint("ordinary", (str(ordinary),))], workers=1).items
        self.live.return_value = ProcessSnapshot(("/opt/homebrew/bin/brew fetch fixture",))
        report = execute_cleanup([other, item], IgnoreRules(), home=self.home)
        self.assertEqual([o.status for o in report.outcomes], ["not_run", "blocked"])
        self.assertTrue(ordinary.exists())
        self.assertFalse(self.trash.exists())

    def test_partial_download_after_preflight_blocks_final_move(self) -> None:
        item = self.item("dev")
        def prepare(_):
            self.write(self.cache / "downloads/new.incomplete")
            self.trash.mkdir(mode=0o700)
            return self.trash
        report = execute_cleanup([item], IgnoreRules(), home=self.home, trash_resolver=prepare)
        self.assertEqual(report.outcomes[0].status, "failed")
        self.assertTrue(self.cache.exists())
        self.assertEqual(list(self.trash.iterdir()), [])

    def test_stopped_cache_still_moves_and_similarly_named_sibling_is_not_owned(self) -> None:
        sibling = self.cache.with_name("Homebrew-backup")
        self.write(sibling / "downloads/example.incomplete")
        self.scanner.return_value = ProcessSnapshot(("brew fetch fixture",))
        sibling_item, = scan_points([ScanPoint("sibling", (str(sibling),))], workers=1).items
        self.assertTrue(sibling_item.actionable)
        self.scanner.return_value = ProcessSnapshot(())
        report = execute_cleanup([self.item("dev")], IgnoreRules(), home=self.home)
        self.assertEqual(report.outcomes[0].status, "moved_to_trash")
        self.assertTrue((self.trash / "Homebrew/downloads/bottle.tar.gz").exists())
        self.assertTrue(sibling.exists())

    def test_environment_cache_is_protected_across_entries_and_after_environment_changes(self) -> None:
        custom = self.home / "Library/Caches/custom-brew"
        self.write(custom / "downloads/bottle.tar.gz")
        with mock.patch.dict(os.environ, {"HOMEBREW_CACHE": str(custom)}):
            self.scanner.return_value = ProcessSnapshot(("brew fetch fixture",))
            for entry in ("dev", "generic", "analyze", "parent", "child"):
                with self.subTest(entry=entry):
                    self.assertFalse(self.item(entry, custom).actionable)
            self.scanner.return_value = ProcessSnapshot(())
            item = self.item("dev", custom)
            self.assertTrue(item.requires_explicit_selection)
            self.assertFalse(item.preselected)
        self.write(custom / "downloads/new.incomplete")
        report = execute_cleanup([item], IgnoreRules(), home=self.home)
        self.assertEqual(report.outcomes[0].status, "blocked")
        self.assertTrue(custom.exists())

    def test_lock_directory_and_ancestors_are_not_cleanable(self) -> None:
        prefix = self.home / "brew-prefix"
        locks = prefix / "var/homebrew/locks"
        self.write(locks / "fixture.lock")
        with mock.patch.dict(os.environ, {"HOMEBREW_PREFIX": str(prefix)}):
            for target in (locks, locks / "fixture.lock", locks.parent, prefix):
                with self.subTest(target=target):
                    item, = scan_points([ScanPoint("scope", (str(target),))], workers=1).items
                    self.assertFalse(item.actionable)
                    self.assertIn("Homebrew 锁目录", item.action_block_reason)

    def test_cli_cannot_force_select_running_cache(self) -> None:
        self.scanner.return_value = ProcessSnapshot(("brew fetch fixture",))
        rules = self.home / "rules.json"
        rules.write_text('{"schema_version":1}')
        output = io.StringIO()
        with mock.patch("openclean.cli.DOMAINS", {"developer": [self.brew]}), contextlib.redirect_stdout(output):
            status = main(["clean", "dev", "--select", str(self.cache), "--yes", "--json", "--rules", str(rules)])
        self.assertEqual(status, 2)
        self.assertIn("selection_error", json.dumps(json.loads(output.getvalue())))
        self.assertTrue(self.cache.exists())


if __name__ == "__main__":
    unittest.main()
