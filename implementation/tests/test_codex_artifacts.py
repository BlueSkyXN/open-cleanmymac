from __future__ import annotations

import io
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from collections import Counter
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from openclean import codex_artifacts as artifacts
from openclean import cleanup as cleanup_module
from openclean.cleanup import execute_cleanup, select_cleanup_items, SelectionError
from openclean.cleanup_guards import CleanupGuardContext
from openclean.cli import _item_payload, main
from openclean.engine import Cancelled, IgnoreRules, finalize_overlapping_result, scan_points
from openclean.models import FileFacts, Item
from openclean.processes import OpenFileDetectionError, OpenFileSnapshot, ProcessSnapshot
from openclean.runtime.finding_projection import item_from_payload, item_to_payload
from openclean.scanpoints import AI_TOOL_JUNK
from openclean.storage_diagnostics import scan_codex_storage_artifact_diagnostics
from openclean.tui import ReviewGroup, _draw_items, _toggle_items


class CodexArtifactTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.root = self.home / ".codex"
        self.root.mkdir()
        self.now = time.time() + 10 * 86400
        self.enterContext(mock.patch.dict(os.environ, {"HOME": str(self.home)}))
        self.protection = IgnoreRules()

    def git(self, name="git-old"):
        path = self.root / ".tmp" / name
        (path / "objects").mkdir(parents=True)
        (path / "refs").mkdir()
        (path / "HEAD").write_text("ref: refs/heads/main\n")
        return path

    def staging(self, name="marketplace-upgrade-old"):
        path = self.root / ".tmp/marketplaces/.staging" / name
        path.mkdir(parents=True)
        return path

    def installed_pair(self, manifest_path=".claude-plugin/marketplace.json"):
        path = self.staging()
        manifest = path / manifest_path
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"name": "sample-market", "plugins": []}))
        metadata = {"source_type": "git", "source": "https://example.invalid/plugins.git",
                    "ref_name": None, "sparse_paths": [], "revision": "a" * 40}
        (path / artifacts._METADATA).write_text(json.dumps(metadata))
        (path / "README.md").write_text("sample marketplace\n")
        installed = self.root / ".tmp/marketplaces/sample-market"
        shutil.copytree(path, installed)
        (self.root / "config.toml").write_text(
            '[marketplaces.sample-market]\nsource_type = "git"\n'
            'source = "https://example.invalid/plugins.git"\n')
        return path, installed

    def scan(self, **kwargs):
        params = {"home": self.home, "process_snapshot": ProcessSnapshot(()),
                  "open_files": OpenFileSnapshot(()), "now": self.now}
        params.update(kwargs)
        return artifacts.scan_codex_transient_targets(self.protection, **params)

    def runner(self, args, **kwargs):
        self.assertIn(args[0], ("/bin/ps", "/usr/sbin/lsof"))
        return subprocess.CompletedProcess(args, 0, "", "")

    def execute(self, items, **kwargs):
        with mock.patch("openclean.codex_artifacts.time.time", return_value=self.now):
            return execute_cleanup(items, self.protection, home=self.home,
                                   process_runner=kwargs.pop("process_runner", self.runner), **kwargs)

    def test_old_skeleton_precise_selection_and_same_volume_trash(self):
        first, second = self.git(), self.git("git-another")
        (first / "objects/.DS_Store").write_text("Finder")
        result = self.scan()
        self.assertTrue(result.complete)
        self.assertEqual(len(result.items), 2)
        self.assertTrue(all(item.actionable for item in result.items))
        self.assertEqual(select_cleanup_items(result.items, include_critical=True), [])
        with self.assertRaisesRegex(SelectionError, "include-critical"):
            select_cleanup_items(result.items, selectors=[str(first)])
        selected = select_cleanup_items(result.items, selectors=[str(first)], include_critical=True)
        self.assertEqual(len(selected), 1)
        item = selected[0]
        self.assertEqual(_item_payload(item)["cleanup_scope"], "codex-transient")
        self.assertEqual(item_from_payload(item_to_payload(item)), item)
        report = self.execute(selected)
        self.assertEqual(report.outcomes[0].status, "moved_to_trash", report.outcomes[0].message)
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())
        self.assertTrue((self.home / ".Trash" / first.name / "HEAD").is_file())

    def test_structure_recent_ctime_and_runtime_guards(self):
        path = self.git()
        cases = [
            ({"now": time.time()}, "静置"),
            ({"process_snapshot": None}, "进程状态未知"),
            ({"process_snapshot": ProcessSnapshot(("/Applications/Codex.app/Contents/MacOS/Codex",))}, "正在运行"),
            ({"open_files": None}, "句柄状态未知"),
            ({"open_files": OpenFileSnapshot((str(path / "HEAD"),))}, "打开句柄"),
        ]
        for params, message in cases:
            with self.subTest(message=message):
                item = self.scan(**params).items[0]
                self.assertFalse(item.actionable)
                self.assertIn(message, item.action_block_reason)
        for filename in ("HEAD", "objects", "refs"):
            os.utime(path / filename, (1, 1))
        os.utime(path, (1, 1))
        self.assertFalse(self.scan(now=time.time()).items[0].actionable,
                         "倒拨 mtime 不能绕过 ctime 静置期")
        for extra in ("objects/ab", "refs/main", "config", "index"):
            with self.subTest(extra=extra):
                (path / extra).write_text("real git data")
                self.assertFalse(self.scan().items[0].actionable)
                (path / extra).unlink()
        for head in ("", "ref: refs/heads/work\n", "a" * 100):
            (path / "HEAD").write_text(head)
            self.assertFalse(self.scan().items[0].actionable)

    def test_protected_child_symlink_hardlink_and_unknown_structure(self):
        path = self.git()
        self.protection = IgnoreRules([str(path / "HEAD")])
        result = self.scan()
        self.assertFalse(result.items)
        self.assertFalse(result.complete)
        self.protection = IgnoreRules()
        (path / "HEAD").unlink()
        (path / "HEAD").symlink_to(self.root / "private")
        self.assertFalse(self.scan().items)
        (path / "HEAD").unlink()
        other = self.root / "shared-head"
        other.write_text("ref: refs/heads/main\n")
        os.link(other, path / "HEAD")
        self.assertFalse(self.scan().items)
        (path / "HEAD").unlink()
        (path / "HEAD").write_text("ref: refs/heads/main\n")
        moved = self.root / "real-tmp"
        (self.root / ".tmp").rename(moved)
        (self.root / ".tmp").symlink_to(moved, target_is_directory=True)
        self.assertFalse(self.scan().items)
        self.assertFalse(self.scan().complete)

    def test_cross_device_owner_and_cloud_checks(self):
        path = self.git()
        context = artifacts.CodexArtifactContext(self.home, self.protection,
            process_snapshot=ProcessSnapshot(()), open_files=OpenFileSnapshot(()), now=self.now)
        value = (path / "HEAD").stat()
        fields = {name: getattr(value, name) for name in (
            "st_mode", "st_size", "st_blocks", "st_dev", "st_ino", "st_uid", "st_nlink")}
        for overrides in ({"st_dev": value.st_dev + 1}, {"st_uid": value.st_uid + 1},
                          {"st_blocks": 0}, {"st_mode": stat.S_IFLNK | 0o777}):
            with self.subTest(overrides=overrides), self.assertRaises(artifacts.ArtifactBlocked):
                context._check(FileFacts(path / "HEAD", SimpleNamespace(**(fields | overrides))), value.st_dev)

    def test_empty_stage_and_complete_duplicate_stage(self):
        empty = self.staging("marketplace-upgrade-empty")
        target, installed = self.installed_pair()
        result = self.scan()
        self.assertEqual({i.path for i in result.items if i.actionable}, {empty, target})
        report = self.execute(result.items)
        self.assertTrue(all(o.status == "moved_to_trash" for o in report.outcomes),
                        [o.message for o in report.outcomes])
        self.assertTrue(installed.is_dir())

    def test_supported_manifests_and_unknown_layout(self):
        for manifest in artifacts._MANIFESTS:
            with self.subTest(manifest=manifest):
                target, installed = self.installed_pair(manifest)
                self.assertTrue(self.scan().items[0].actionable)
                shutil.rmtree(target)
                shutil.rmtree(installed)
        self.installed_pair(".github/plugin/marketplace.json")
        self.assertFalse(self.scan().items[0].actionable)

    def test_stage_must_match_config_install_and_all_content(self):
        target, installed = self.installed_pair()
        mutations = [
            (target / "README.md", "modified"),
            (installed / artifacts._METADATA, "{}"),
            (self.root / "config.toml", '[marketplaces.sample-market]\nsource_type="git"\nsource="elsewhere"'),
            (target / ".claude-plugin/marketplace.json", '{"name":"wrong","plugins":[]}'),
        ]
        for path, contents in mutations:
            with self.subTest(path=path):
                before = path.read_bytes()
                path.write_text(contents)
                self.assertFalse(self.scan().items[0].actionable)
                path.write_bytes(before)
        extra = target / "my-notes.md"
        extra.write_text("keep user content")
        self.assertFalse(self.scan().items[0].actionable)
        extra.unlink()
        shutil.rmtree(installed)
        self.assertFalse(self.scan().items[0].actionable)

    def test_config_errors_profile_references_and_secret_redaction(self):
        target = self.staging()
        config = self.root / "direct.config.toml"
        for value in (f'model_instructions_file = "{target}/prompt.md"',
                      'model_instructions_file = ".tmp/marketplaces/.staging"',
                      f'[marketplaces.custom]\nsource_type="local"\nsource="{target}"',
                      'token = "SECRET_MALFORMED', "x" * (artifacts._MAX_JSON_BYTES + 1)):
            config.write_text(value)
            result = self.scan()
            self.assertFalse(result.items[0].actionable)
            self.assertNotIn("SECRET_MALFORMED", result.items[0].action_block_reason)
        config.unlink()
        config.symlink_to(self.root / "unread")
        self.assertFalse(self.scan().items[0].actionable)

    def test_normal_codex_home_and_cwd_settings_are_not_temp_references(self):
        self.git()
        (self.root / "direct.config.toml").write_text(
            f'[mcp_servers.sample]\ncwd="."\n[mcp_servers.sample.env]\nCODEX_HOME="{self.root}"\n')
        self.assertTrue(self.scan().items[0].actionable)
        (self.root / "direct.config.toml").write_text('[mcp_servers.sample]\ncwd=".tmp"\n')
        self.assertFalse(self.scan().items[0].actionable)

    def test_other_codex_materials_and_backups_are_not_candidates(self):
        keep = ["AGENTS-example.md", "config-example.toml", "model_catalog.override.json.example.json",
                "worktrees/project/notes.md", ".tmp/marketplaces/marketplace-backup-old/root/data",
                "packages/app-server-daemon/releases/version/bin/codex", "suites/github/README.md"]
        for relative in keep:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("keep")
        self.staging()
        result = self.scan()
        self.assertEqual(len(result.items), 1)
        self.assertTrue(result.items[0].actionable)
        for relative in keep:
            self.assertTrue((self.root / relative).exists())

    def test_deeply_nested_config_blocks_all_targets_without_reparsing(self):
        first, second = self.git(), self.git("git-second")
        config = self.root / "config.toml"
        config.write_text('private_value = ' + '[' * 1500 + '"SECRET"' + ']' * 1500)
        with mock.patch.object(artifacts.tomllib, "loads", wraps=artifacts.tomllib.loads) as parse:
            result = self.scan()
        self.assertFalse(result.complete)
        self.assertEqual(parse.call_count, 1)
        self.assertEqual({item.path for item in result.items}, {first, second})
        self.assertTrue(all(not item.actionable for item in result.items))
        self.assertNotIn("SECRET", str(result.issues))

    def test_forged_flags_generic_parent_and_internal_leaf_cannot_bypass(self):
        path = self.git()
        item = self.scan().items[0]
        candidates = [replace(item, cleanup_scope="", cleanup_root=None, cleanup_root_identity=None),
                      replace(item, requires_explicit_selection=False), replace(item, safety="safe"),
                      replace(item, preselected=True)]
        for target in (self.root, self.root / ".tmp", path / "HEAD", path / "objects"):
            candidates.append(Item(target, 0, "generic Analyze", identity=FileFacts.from_path(target).identity))
        for forged in candidates:
            with self.subTest(path=forged.path, scope=forged.cleanup_scope):
                report = self.execute([forged])
                self.assertEqual(report.outcomes[0].status, "blocked")
                self.assertTrue((path / "HEAD").exists())
        guard = CleanupGuardContext(self.protection, lambda: ProcessSnapshot(()), home=self.home)
        self.assertTrue(guard.assess(path).block_reason)

    def test_scan_then_mutation_and_changed_runtime_block_batch(self):
        path = self.git()
        second = self.git("git-second")
        items = self.scan().items
        (path / "config").write_text("real repository now")
        report = self.execute(items)
        self.assertEqual({o.status for o in report.outcomes}, {"blocked", "not_run"})
        self.assertTrue(second.exists())
        (path / "config").unlink()
        def running(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, "Codex.app" if args[0] == "/bin/ps" else "", "")
        self.assertEqual(self.execute(items, process_runner=running).outcomes[0].status, "blocked")
        with mock.patch("openclean.cleanup.capture_open_file_snapshot", side_effect=OpenFileDetectionError("unknown")):
            self.assertEqual(self.execute(items).outcomes[0].status, "blocked")
        with mock.patch("openclean.cleanup.capture_open_file_snapshot", return_value=OpenFileSnapshot((str(path / "HEAD"),))):
            self.assertIn("blocked", [o.status for o in self.execute(items).outcomes])

    def test_mutation_during_trash_preparation_is_caught(self):
        path = self.git()
        item = self.scan().items[0]
        trash = self.home / ".Trash"
        trash.mkdir(mode=0o700)
        def resolver(_path):
            (path / "objects/real-data").write_text("preserve")
            return trash
        report = self.execute([item], trash_resolver=resolver)
        self.assertEqual(report.outcomes[0].status, "failed")
        self.assertTrue((path / "objects/real-data").exists())
        self.assertEqual(list(trash.iterdir()), [])

    def test_final_guard_rechecks_after_source_directory_is_open(self):
        path = self.git()
        item = self.scan().items[0]
        original = cleanup_module._stat_at
        def changed(parent_fd, name, target):
            result = original(parent_fd, name, target)
            if target == path:
                (path / "config").write_text("created after directory fd opened")
            return result
        with mock.patch("openclean.cleanup._stat_at", side_effect=changed):
            report = self.execute([item])
        self.assertEqual(report.outcomes[0].status, "failed")
        self.assertTrue((path / "config").exists())

    def test_case_alias_does_not_escape_special_scope(self):
        path = self.git()
        alias = self.home / ".CoDeX/.TmP/GIT-old"
        if not alias.exists():
            self.skipTest("当前临时卷大小写敏感；别名无对应对象")
        guard = CleanupGuardContext(self.protection, lambda: ProcessSnapshot(()), home=self.home)
        self.assertTrue(guard.assess(alias).block_reason)
        item = Item(alias, 0, "generic", identity=FileFacts.from_path(alias).identity)
        self.assertEqual(self.execute([item]).outcomes[0].status, "blocked")
        self.assertTrue(path.exists())

    def test_stage_content_and_config_changes_before_execution_block(self):
        target, _ = self.installed_pair()
        item = self.scan().items[0]
        (target / "README.md").write_text("keep edited notes")
        self.assertEqual(self.execute([item]).outcomes[0].status, "blocked")
        (target / "README.md").write_text("sample marketplace\n")
        with (self.root / "config.toml").open("a") as stream:
            stream.write(f'\n[marketplaces.local]\nsource_type="local"\nsource="{target}"\n')
        self.assertEqual(self.execute([item]).outcomes[0].status, "blocked")

    def test_limits_and_invalid_config_report_incomplete(self):
        self.staging("marketplace-upgrade-empty")
        (self.root / "config.toml").write_text('marketplaces = "invalid"')
        self.assertFalse(self.scan().complete)
        (self.root / "config.toml").unlink()
        target, _ = self.installed_pair()
        with mock.patch("openclean.codex_artifacts._MAX_COMPARE_BYTES", 1):
            result = self.scan()
        self.assertFalse(result.complete)
        self.assertFalse(next(i for i in result.items if i.path == target).actionable)

    def test_git_block_reasons_and_counts_explain_structure(self):
        good = self.git("git-good")
        missing = self.git("git-missing")
        (missing / "HEAD").unlink()
        empty = self.git("git-empty")
        (empty / "HEAD").write_text("")
        unexpected = self.git("git-unexpected")
        (unexpected / "HEAD").write_text("ref: refs/heads/work\n")
        data = self.git("git-data")
        (data / "objects/hash").write_text("real object")
        extra = self.git("git-extra")
        (extra / "config").write_text("keep config")
        wrong_type = self.git("git-type")
        (wrong_type / "refs").rmdir()
        (wrong_type / "refs").write_text("not directory")
        result = self.scan()
        reasons = result.diagnostics[0]["block_reasons"]
        self.assertEqual({r["code"] for r in reasons}, {
            "git_entries_missing", "git_head_empty", "git_head_unexpected", "git_data_present",
            "git_extra_entries", "git_directory_type_invalid",
        })
        self.assertEqual(sum(r["count"] for r in reasons), 6)
        self.assertEqual([i.path for i in result.items if i.actionable], [good])
        self.assertFalse(any("不是已知的 Git 空骨架" in i.action_block_reason for i in result.items))

    def test_marketplace_missing_metadata_revision_and_content_are_distinct(self):
        target, installed = self.installed_pair()
        metadata_path = target / artifacts._METADATA
        original = metadata_path.read_text()
        metadata_path.unlink()
        result = self.scan()
        self.assertEqual(result.diagnostics[1]["block_reasons"][0]["code"], "marketplace_metadata_missing")
        self.assertFalse(result.complete)
        metadata_path.write_text(original)
        modified = json.loads(original)
        modified["revision"] = "b" * 40
        metadata_path.write_text(json.dumps(modified))
        self.assertEqual(self.scan().diagnostics[1]["block_reasons"][0]["code"], "marketplace_revision_mismatch")
        metadata_path.write_text(original)
        (target / "my-notes").write_text("preserve")
        self.assertEqual(self.scan().diagnostics[1]["block_reasons"][0]["code"], "marketplace_content_differs")
        shutil.rmtree(installed)
        self.assertEqual(self.scan().diagnostics[1]["block_reasons"][0]["code"], "marketplace_install_missing")

    def test_batch_trash_retains_per_target_fresh_runtime_checks(self):
        for index in range(64):
            self.git(f"git-{index:03d}")
        items = self.scan().items
        calls = Counter()
        def runner(command, **kwargs):
            calls[Path(command[0]).name] += 1
            return self.runner(command, **kwargs)
        report = self.execute(items, process_runner=runner)
        self.assertEqual([o.status for o in report.outcomes], ["moved_to_trash"] * 64)
        self.assertEqual(calls, {"ps": 129, "lsof": 129})
        self.assertEqual(len(list((self.home / ".Trash").iterdir())), 64)
        self.assertEqual(list((self.root / ".tmp").iterdir()), [])

    def test_runtime_start_before_second_target_final_check_stops_that_target(self):
        first, second = self.git("git-a"), self.git("git-b")
        items = self.scan().items
        ps_calls = 0
        def runner(command, **kwargs):
            nonlocal ps_calls
            if command[0] == "/bin/ps":
                ps_calls += 1
                return subprocess.CompletedProcess(command, 0, "Codex.app" if ps_calls >= 5 else "", "")
            return self.runner(command, **kwargs)
        report = self.execute(items, process_runner=runner)
        self.assertEqual([o.status for o in report.outcomes], ["moved_to_trash", "failed"])
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())

    def test_open_handle_created_after_trash_preparation_blocks_final_move(self):
        path = self.git()
        item = self.scan().items[0]
        lsof_calls = 0
        def runner(command, **kwargs):
            nonlocal lsof_calls
            if command[0] == "/usr/sbin/lsof":
                lsof_calls += 1
                return subprocess.CompletedProcess(command, 0, f"n{path}/HEAD\n" if lsof_calls >= 3 else "", "")
            return self.runner(command, **kwargs)
        report = self.execute([item], process_runner=runner)
        self.assertEqual(report.outcomes[0].status, "failed")
        self.assertIn("句柄", report.outcomes[0].message)
        self.assertTrue(path.exists())

    def test_truncation_and_cancellation(self):
        for name in ("git-a", "git-b", "git-c"):
            self.git(name)
        with mock.patch.dict(artifacts.MAX_TARGETS, {"git": 2}):
            result = self.scan()
        self.assertFalse(result.complete)
        self.assertEqual(len(result.items), 2)
        self.assertTrue(all(not i.actionable for i in result.items))
        self.assertEqual(result.diagnostics[0]["total_count"], 3)
        cancelled = self.scan(checkpoint=mock.Mock(side_effect=Cancelled))
        self.assertTrue(cancelled.cancelled)
        self.assertFalse(cancelled.complete)
        progressed = 0
        def progress():
            nonlocal progressed
            progressed += 1
        def checkpoint():
            if progressed == 1:
                raise Cancelled
        cancelled = self.scan(checkpoint=checkpoint, on_progress=progress)
        self.assertEqual(len(cancelled.items), 1)
        self.assertTrue(cancelled.cancelled)
        self.assertFalse(cancelled.items[0].actionable)
        self.assertEqual(cancelled.diagnostics[0]["actionable_count"], 0)
        self.assertEqual(cancelled.diagnostics[0]["block_reasons"][0]["code"], "scan_cancelled")

    def test_classic_dispatch_summary_not_double_counted_and_agent_aggregate_stays_readonly(self):
        self.git()
        self.staging()
        runtime = (ProcessSnapshot(()), OpenFileSnapshot(()))
        with mock.patch("openclean.storage_diagnostics._capture_runtime_snapshots", return_value=runtime), \
             mock.patch("openclean.codex_artifacts.time.time", return_value=self.now):
            exact = scan_codex_storage_artifact_diagnostics(self.protection, exact_targets=True)
            legacy = scan_codex_storage_artifact_diagnostics(self.protection)
            point = next(p for p in AI_TOOL_JUNK if p.scanner == "codex-storage-artifacts")
            dispatched = scan_points((point,), ignore=self.protection)
        self.assertEqual(len(exact.items), 2)
        self.assertTrue(all(i.actionable for i in dispatched.items))
        self.assertEqual(finalize_overlapping_result(exact).total, exact.total)
        self.assertTrue(all(i.resource_kind == "filesystem" for i in exact.items))
        self.assertTrue(all(not i.actionable for i in legacy.items))
        self.assertTrue(exact.diagnostics)

    def test_clean_preview_without_yes_never_moves_targets(self):
        path = self.git()
        result = self.scan()
        output = io.StringIO()
        with mock.patch("openclean.cli.scan_domains", return_value=result), \
             mock.patch("openclean.cli.execute_cleanup", side_effect=AssertionError("preview must not execute")), \
             redirect_stdout(output):
            code = main(["clean", "ai", "--json", "--select", str(path), "--include-critical"])
        self.assertEqual(code, 0, output.getvalue())
        self.assertTrue(path.exists())
        json.loads(output.getvalue())

    def test_selection_file_runs_codex_scope_and_preserves_unselected_target(self):
        first, second, keep = self.git("git-first"), self.git("git-second"), self.git("git-keep")
        selection = self.home / "targets.json"
        selection.write_text(json.dumps([str(first), str(second)]))
        rules = self.home / "rules.json"
        rules.write_text('{"schema_version":1}')
        result = self.scan()
        output = io.StringIO()
        with mock.patch("openclean.cli.scan_domains", return_value=result), \
                mock.patch("openclean.cleanup.capture_process_snapshot", return_value=ProcessSnapshot(())), \
                mock.patch("openclean.cleanup.capture_open_file_snapshot", return_value=OpenFileSnapshot(())), \
                mock.patch("openclean.codex_artifacts.time.time", return_value=self.now), redirect_stdout(output):
            code = main(["clean", "ai", "--select-file", str(selection), "--include-critical", "--yes",
                         "--json", "--rules", str(rules)])
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0, payload)
        self.assertEqual([o["status"] for o in payload["cleanup"]["outcomes"]], ["moved_to_trash"] * 2)
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())
        self.assertTrue(keep.exists())
        self.assertEqual(len(list((self.home / ".Trash").iterdir())), 2)

    def test_twenty_thousand_skeletons_are_covered_and_measured_once(self):
        for index in range(20000):
            self.git(f"git-{index:05d}")
        start = time.monotonic()
        result = self.scan()
        elapsed = time.monotonic() - start
        self.assertTrue(result.complete)
        self.assertEqual(len(result.items), 20000)
        self.assertTrue(all(i.actionable and not i.preselected for i in result.items))
        self.assertEqual(result.diagnostics[0]["checked_count"], 20000)
        self.assertEqual(result.total, sum(i.allocated_size for i in result.items))
        self.assertEqual(len(select_cleanup_items(result.items, selectors=[str(result.items[0].path)],
                                                  include_critical=True)), 1)
        start = time.monotonic()
        finalized = finalize_overlapping_result(result)
        self.assertEqual(len(finalized.items), 20000)
        self.assertEqual(finalized.total, result.total)
        serialized = json.dumps([_item_payload(i) for i in finalized.items], ensure_ascii=False)
        self.assertEqual(len(json.loads(serialized)), 20000)
        serialized_bytes = len(serialized.encode("utf-8"))
        output_elapsed = time.monotonic() - start
        selected = set()
        _toggle_items(result.items, selected)
        self.assertEqual(selected, set(), "批量选择不应包含精确目标")
        groups = (ReviewGroup("ai", "Codex", tuple(finalized.items)),)
        screen = mock.Mock()
        screen.getmaxyx.return_value = (30, 120)
        start = time.monotonic()
        _draw_items(screen, groups, 0, 19999, selected, "清理审阅")
        draw_elapsed = time.monotonic() - start
        self.assertLess(screen.addnstr.call_count, 100, "列表只绘制当前可见窗口")
        print(f"\nCodex synthetic: scan={elapsed:.2f}s; finalize+JSON={output_elapsed:.2f}s; "
              f"JSON={serialized_bytes}B; TUI window={draw_elapsed:.3f}s; 20000 targets")


if __name__ == "__main__":
    unittest.main()
