from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from openclean.analyzer import analyze_path
from openclean.cleanup import execute_cleanup, select_cleanup_items
from openclean.cli import main
from openclean.engine import Cancelled, Control, IgnoreRules, scan_project_artifacts
from openclean.knowledge_base import KnowledgeBase
from openclean.macos import filesystem_case_sensitive
from openclean.models import FileFacts
from openclean.processes import ProcessSnapshot
from openclean.project_artifacts import assess_project_artifact, project_scope_block_reason


class ProjectContentGuardsTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.project = self.home / "project"
        self.project.mkdir()
        (self.project / "package.json").write_text("{}")
        self.template = self.home / "empty-template"
        self.template.mkdir()
        self.rules = self.home / "rules.json"
        self.rules.write_text('{"schema_version":1}')
        self.trash = self.home / ".Trash"
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(os.environ, {"HOME": str(self.home)}, clear=True).start()
        mock.patch("openclean.engine.capture_process_snapshot", return_value=ProcessSnapshot(())).start()
        mock.patch("openclean.cleanup.capture_process_snapshot", return_value=ProcessSnapshot(())).start()

    def git(self, root: Path, *args: str) -> str:
        return subprocess.run(
            ["/usr/bin/git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
             "-c", "commit.gpgsign=false", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
             "-C", str(root), *args],
            env={"PATH": "/usr/bin:/bin", "HOME": str(self.home), "GIT_CONFIG_NOSYSTEM": "1",
                 "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_OPTIONAL_LOCKS": "0"},
            check=True, capture_output=True, text=True,
        ).stdout

    def init(self, root: Path) -> None:
        self.git(root, "init", "-q", f"--template={self.template}")

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic fixture\n" * 512)
        return path

    def age(self, root: Path) -> None:
        old = time.time() - 10 * 86400
        for directory, dirs, files in os.walk(root, topdown=False):
            for name in files + dirs:
                os.utime(Path(directory) / name, (old, old), follow_symlinks=False)
            os.utime(directory, (old, old))

    def scan(self):
        self.age(self.project)
        return scan_project_artifacts([self.project])

    def test_key_git_directory_and_gitfile_block_default_and_explicit_selection(self) -> None:
        for kind in ("key", "git-directory", "git-file"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(dir=self.home) as tmp:
                project = Path(tmp)
                (project / "package.json").write_text("{}")
                artifact = project / "target"
                self.write(artifact / "output.bin")
                if kind == "key":
                    self.write(artifact / "deploy/demo-keypair.json")
                elif kind == "git-directory":
                    self.init(artifact)
                else:
                    (artifact / ".git").write_text("gitdir: missing\n")
                self.age(artifact)
                result = scan_project_artifacts([project])
                item, = result.items
                self.assertTrue(result.complete)
                self.assertFalse(item.actionable)
                self.assertFalse(item.preselected)
                self.assertTrue(item.action_block_reason)
                self.assertEqual(select_cleanup_items(result.items, select_all_safe=True), [])
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    status = main(["purge", str(project), "--select", str(artifact), "--yes",
                                   "--json", "--rules", str(self.rules)])
                self.assertEqual(status, 2)
                self.assertIn("selection_error", stdout.getvalue())
                self.assertTrue(artifact.exists())
                self.assertFalse(self.trash.exists())

    def test_tracked_vendor_in_parent_repository_and_staged_files_are_protected(self) -> None:
        self.init(self.home)
        tracked = self.write(self.project / "vendor/handwritten.php")
        self.git(self.home, "add", "--", str(tracked))
        result = self.scan()
        self.assertIn("Git 索引跟踪", result.items[0].action_block_reason)
        self.assertFalse(result.items[0].actionable)

    def test_inner_repository_does_not_hide_outer_tracked_source(self) -> None:
        self.init(self.home)
        source = self.write(self.project / "vendor/source.php")
        self.age(self.project)
        before, = scan_project_artifacts([self.project]).items
        self.assertTrue(before.actionable)
        self.git(self.home, "add", "--", "project/vendor/source.php")
        self.init(self.project)
        self.assertIn("project/vendor/source.php", self.git(self.home, "ls-files", "--cached"))
        source.write_bytes(b"uncommitted source changes\n" * 512)
        result = self.scan()
        item, = result.items
        self.assertTrue(result.complete)
        self.assertFalse(item.actionable)
        self.assertFalse(item.preselected)
        self.assertIn("Git 索引跟踪", item.action_block_reason)
        self.assertEqual(self.git(self.project, "ls-files", "--cached", "--", "vendor/"), "")
        report = execute_cleanup([before], IgnoreRules(), home=self.home)
        self.assertEqual(report.outcomes[0].status, "blocked")
        self.assertEqual(source.read_bytes(), b"uncommitted source changes\n" * 512)
        self.assertFalse(self.trash.exists())

    def test_outer_repository_probe_failure_is_not_hidden_by_inner_empty_index(self) -> None:
        self.init(self.home)
        self.init(self.project)
        self.write(self.project / "vendor/source.php")
        (self.home / ".git/index").write_bytes(b"invalid index")
        result = self.scan()
        self.assertFalse(result.complete)
        self.assertFalse(result.items[0].actionable)
        self.assertIn("project_content_check_failed", {i.code for i in result.issues})

    def test_untracked_artifact_in_nested_repositories_remains_executable(self) -> None:
        self.init(self.home)
        self.init(self.project)
        self.write(self.project / "vendor/output.bin")
        item, = self.scan().items
        self.assertTrue(item.actionable)
        self.assertTrue(item.preselected)
        report = execute_cleanup([item], IgnoreRules(), home=self.home)
        self.assertEqual(report.outcomes[0].status, "moved_to_trash")
        self.assertTrue((self.home / ".git").is_dir())
        self.assertTrue((self.project / ".git").is_dir())

    def test_case_changed_index_path_blocks_scan_and_execution(self) -> None:
        self.init(self.home)
        project = self.home / "Packages/Web[1]"
        source = self.write(project / "vendor/source.php")
        (project / "package.json").write_text("{}")
        self.git(self.home, "add", "--", "Packages/Web[1]/vendor/source.php")
        upper = self.home / "Packages"
        upper.rename(self.home / "intermediate-name")
        (self.home / "intermediate-name").rename(self.home / "packages")
        project = self.home / "packages/Web[1]"
        source = project / "vendor/source.php"
        source.write_bytes(b"uncommitted source changes\n" * 512)
        self.age(project)
        self.assertEqual(self.git(self.home, "ls-files", "--cached", "--",
                                  ":(top,literal)packages/Web[1]/vendor/"), "")
        # 在大小写敏感 runner 上也测试不敏感分支；macOS 默认卷走真实 pathconf。
        mode = (mock.patch("openclean.project_artifacts.filesystem_case_sensitive", return_value=False)
                if filesystem_case_sensitive(self.home) else contextlib.nullcontext())
        with mode, mock.patch.dict(os.environ, {"GIT_LITERAL_PATHSPECS": "1"}):
            item, = scan_project_artifacts([project]).items
            self.assertFalse(item.actionable)
            self.assertFalse(item.preselected)
            self.assertIn("Git 索引跟踪", item.action_block_reason)
            stale = replace(item, actionable=True, preselected=True, action_block_reason="")
            report = execute_cleanup([stale], IgnoreRules(), home=self.home)
        self.assertEqual(report.outcomes[0].status, "blocked")
        self.assertTrue(source.exists())
        self.assertFalse(self.trash.exists())

    def test_case_sensitive_path_does_not_match_a_different_index_directory(self) -> None:
        self.init(self.home)
        upper = self.home / "Packages/Web[1]"
        self.write(upper / "vendor/source.php")
        self.git(self.home, "add", "--", "Packages/Web[1]/vendor/source.php")
        if not filesystem_case_sensitive(self.home):
            (self.home / "Packages").rename(self.home / "other-working-directory")
        lower = self.home / "packages/Web[1]"
        self.write(lower / "vendor/output.bin")
        (lower / "package.json").write_text("{}")
        # 索引中相异大小写的目录不能仅因 icase 扩展而阻断敏感文件系统。
        with mock.patch("openclean.project_artifacts.filesystem_case_sensitive", return_value=True):
            item, = scan_project_artifacts([lower]).items
        self.assertTrue(item.actionable)
        self.assertEqual(item.action_block_reason, "")

    def test_icase_pathspec_does_not_expand_literal_metacharacters(self) -> None:
        self.init(self.home)
        for name in ("Web1", "Web[1]"):
            project = self.home / name
            self.write(project / "vendor/source.php")
            (project / "package.json").write_text("{}")
        self.git(self.home, "add", "--", "Web1/vendor/source.php")
        with mock.patch("openclean.project_artifacts.filesystem_case_sensitive", return_value=False):
            item, = scan_project_artifacts([self.home / "Web[1]"]).items
        self.assertTrue(item.actionable)

    def test_unknown_filesystem_case_semantics_fail_closed(self) -> None:
        self.init(self.project)
        self.write(self.project / "vendor/output.bin")
        with mock.patch("openclean.project_artifacts.filesystem_case_sensitive", side_effect=OSError("unsupported")):
            result = self.scan()
        self.assertFalse(result.complete)
        self.assertFalse(result.items[0].actionable)
        with mock.patch("openclean.macos.os.pathconf", return_value=-1):
            with self.assertRaises(OSError):
                filesystem_case_sensitive(self.home)

    def test_untracked_artifact_in_git_repository_remains_executable(self) -> None:
        self.init(self.project)
        self.git(self.project, "add", "package.json")
        self.write(self.project / "target/output.bin")
        item, = self.scan().items
        self.assertTrue(item.actionable)
        self.assertTrue(item.preselected)
        report = execute_cleanup([item], IgnoreRules(), home=self.home)
        self.assertEqual(report.outcomes[0].status, "moved_to_trash")
        self.assertTrue((self.project / "package.json").exists())
        self.assertTrue((self.project / ".git").is_dir())

    def test_git_environment_and_fsmonitor_cannot_redirect_or_execute(self) -> None:
        self.init(self.project)
        tracked = self.write(self.project / "vendor/tracked.txt")
        self.git(self.project, "add", "--", str(tracked))
        hook = self.home / "monitor.sh"
        sentinel = self.home / "executed"
        hook.write_text(f"#!/bin/sh\ntouch '{sentinel}'\n")
        hook.chmod(0o700)
        self.git(self.project, "config", "core.fsmonitor", str(hook))
        with mock.patch.dict(os.environ, {"GIT_DIR": str(self.home / "wrong"),
                                         "GIT_INDEX_FILE": str(self.home / "missing-index"),
                                         "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.fsmonitor",
                                         "GIT_CONFIG_VALUE_0": str(hook)}):
            result = self.scan()
        self.assertIn("Git 索引跟踪", result.items[0].action_block_reason)
        self.assertFalse(sentinel.exists())
        self.assertFalse((self.project / ".git/index.lock").exists())

    def test_linked_worktrees_are_opt_in_and_preserve_source_and_gitfile(self) -> None:
        self.init(self.project)
        self.git(self.project, "add", "package.json")
        self.git(self.project, "commit", "-qm", "Fixture")
        for tool in ("codex", "claude"):
            with self.subTest(tool=tool):
                root = (self.home / ".codex/worktrees" if tool == "codex"
                        else self.project / ".claude/worktrees")
                root.mkdir(parents=True)
                checkout = root / "sample"
                self.git(self.project, "worktree", "add", "-q", "--detach", str(checkout))
                self.write(checkout / "node_modules/output.bin")
                self.age(checkout / "node_modules")
                item, = scan_project_artifacts([root], include_unmarked_roots=True).items
                self.assertTrue(item.actionable)
                report = execute_cleanup([item], IgnoreRules(), home=self.home)
                self.assertEqual(report.outcomes[0].status, "moved_to_trash")
                self.assertTrue((checkout / ".git").is_file())
                self.assertTrue((checkout / "package.json").exists())

    def test_dependency_root_and_its_descendant_are_not_project_search_roots(self) -> None:
        package = self.project / "node_modules/package"
        self.write(package / "package.json")
        self.write(package / "target/output.bin")
        for root in (package.parent, package, package / "target"):
            with self.subTest(root=root):
                result = scan_project_artifacts([root], include_unmarked_roots=True)
                self.assertEqual(result.items, [])
                self.assertEqual(result.issues[0].code, "unsafe_project_search_root")
                self.assertFalse(result.complete)
        self.assertEqual([i.path for i in self.scan().items], [package.parent])

    def test_case_alias_dependency_roots_are_blocked_in_scan_and_cli(self) -> None:
        if filesystem_case_sensitive(self.home):
            self.skipTest("真实大小写别名需不敏感文件系统")
        for real, alias in (("node_modules", "NODE_MODULES"), ("vendor", "VENDOR"),
                            ("cmake-build-debug", "CMAKE-BUILD-DEBUG"),
                            (".vitepress/dist", ".VITEPRESS/DIST")):
            package = self.project / real / "pkg"
            self.write(package / "package.json")
            source = self.write(package / "target/runtime.js")
            root = self.project / alias / "pkg"
            self.assertEqual(root.stat().st_ino, package.stat().st_ino)
            with self.subTest(alias=alias):
                result = scan_project_artifacts([root], include_unmarked_roots=True)
                self.assertFalse(result.complete)
                self.assertEqual(result.items, [])
                self.assertEqual(result.issues[0].code, "unsafe_project_search_root")
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    status = main(["purge", str(root), "--all", "--yes", "--json", "--rules", str(self.rules)])
                self.assertEqual(status, 1)
                self.assertTrue(source.exists())
                self.assertFalse(self.trash.exists())

    def test_case_alias_dependency_scope_is_rechecked_for_stale_selection(self) -> None:
        if filesystem_case_sensitive(self.home):
            self.skipTest("真实大小写别名需不敏感文件系统")
        self.write(self.project / "target/runtime.js")
        item, = self.scan().items
        nested = self.home / "outer/node_modules/pkg"
        nested.parent.mkdir(parents=True)
        self.project.rename(nested)
        alias = self.home / "outer/NODE_MODULES/pkg"
        stale = replace(item, path=alias / "target", project_root=alias)
        report = execute_cleanup([stale], IgnoreRules(), home=self.home)
        self.assertEqual(report.outcomes[0].status, "blocked")
        self.assertTrue((nested / "target/runtime.js").exists())
        self.assertFalse(self.trash.exists())

    def test_sensitive_scope_and_similar_names_are_not_blanket_blocked(self) -> None:
        package = self.project / "NODE_MODULES/pkg"
        self.write(package / "package.json")
        self.write(package / "target/runtime.js")
        with mock.patch("openclean.macos.filesystem_case_sensitive", return_value=True):
            self.assertEqual(project_scope_block_reason(package), "")
            result = scan_project_artifacts([package], include_unmarked_roots=True)
        self.assertTrue(result.complete)
        self.assertTrue(result.items[0].actionable)
        for name in ("NODE_MODULES-backup", "VENDOR-src", ".VITEPRESS/theme"):
            with self.subTest(name=name):
                self.assertEqual(project_scope_block_reason(self.project / name / "pkg"), "")
        with mock.patch("openclean.macos.filesystem_case_sensitive", return_value=False):
            self.assertTrue(project_scope_block_reason(package))

    def test_case_scope_unknown_symlink_cloud_and_protected_parents_fail_closed(self) -> None:
        package = self.project / "NODE_MODULES/pkg"
        self.write(package / "package.json")
        self.write(package / "target/runtime.js")
        with mock.patch("openclean.macos.filesystem_case_sensitive", side_effect=OSError("unknown")):
            result = scan_project_artifacts([package], include_unmarked_roots=True)
        self.assertFalse(result.complete)
        self.assertEqual(result.items, [])
        self.assertIn("无法完成", result.issues[0].message)
        for kind in ("protected", "cloud", "symlink"):
            with self.subTest(kind=kind), contextlib.ExitStack() as stack:
                protection = IgnoreRules()
                if kind == "protected":
                    knowledge = KnowledgeBase.from_mapping({"schema_version": 1, "protect": {"paths": [str(self.project)]}})
                    protection = IgnoreRules(knowledge_base=knowledge)
                elif kind == "cloud":
                    stack.enter_context(mock.patch.object(FileFacts, "is_probable_cloud_placeholder",
                                                          new=property(lambda facts: facts.path == self.project)))
                else:
                    moved = self.home / "elsewhere"
                    self.project.rename(moved)
                    self.project.symlink_to(moved, target_is_directory=True)
                query = stack.enter_context(mock.patch("openclean.macos.filesystem_case_sensitive"))
                result = scan_project_artifacts([package], ignore=protection, include_unmarked_roots=True)
                self.assertFalse(result.complete)
                self.assertEqual(result.items, [])
                query.assert_not_called()

    def test_content_added_after_scan_blocks_entire_batch(self) -> None:
        self.write(self.project / "target/output.bin")
        self.write(self.project / ".pytest_cache/cache.bin")
        items = self.scan().items
        self.write(self.project / "target/deploy/new-keypair.json")
        report = execute_cleanup(items, IgnoreRules(), home=self.home)
        self.assertEqual({o.status for o in report.outcomes}, {"blocked", "not_run"})
        self.assertFalse(self.trash.exists())
        self.assertTrue(all(i.path.exists() for i in items))

    def test_content_added_after_preflight_blocks_final_move(self) -> None:
        artifact = self.project / "target"
        self.write(artifact / "output.bin")
        item, = self.scan().items
        def prepare(_):
            self.write(artifact / "new-keypair.json")
            self.trash.mkdir(mode=0o700)
            return self.trash
        report = execute_cleanup([item], IgnoreRules(), home=self.home, trash_resolver=prepare)
        self.assertEqual(report.outcomes[0].status, "failed")
        self.assertTrue(artifact.exists())
        self.assertEqual(list(self.trash.iterdir()), [])

    def test_git_probe_errors_and_unreadable_index_fail_closed(self) -> None:
        self.init(self.project)
        self.write(self.project / "target/output.bin")
        with mock.patch("openclean.project_artifacts.subprocess.Popen", side_effect=FileNotFoundError("git")):
            result = self.scan()
        self.assertFalse(result.complete)
        self.assertFalse(result.items[0].actionable)
        self.assertEqual(result.issues[0].code, "project_content_check_failed")
        (self.project / ".git/index").write_bytes(b"invalid index")
        result = self.scan()
        self.assertFalse(result.complete)
        self.assertFalse(result.items[0].actionable)

    def test_git_probe_timeout_reaps_child_and_reports_unknown(self) -> None:
        self.init(self.project)
        self.write(self.project / "target/output.bin")
        original_popen = subprocess.Popen
        children = []
        def stalled(*args, **kwargs):
            child = original_popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            children.append(child)
            return child
        started = time.monotonic()
        with mock.patch("openclean.project_artifacts.subprocess.Popen", side_effect=stalled):
            result = assess_project_artifact(self.project / "target", self.project, IgnoreRules(), timeout=0.2)
        self.assertFalse(result.complete)
        self.assertTrue(children)
        self.assertTrue(all(child.poll() is not None for child in children))
        self.assertLess(time.monotonic() - started, 3)

    def test_git_probe_output_is_bounded_and_pathspec_is_literal(self) -> None:
        self.init(self.home)
        special = self.home / "literal[1]"
        special.mkdir()
        (special / "package.json").write_text("{}")
        tracked = self.write(special / "vendor/a.txt")
        self.git(self.home, "add", "--", str(tracked))
        item, = scan_project_artifacts([special]).items
        self.assertFalse(item.actionable)
        original_popen = subprocess.Popen
        children = []
        def verbose(*args, **kwargs):
            child = original_popen([sys.executable, "-c", "import os,time; os.write(1,b'x'*65536); time.sleep(30)"],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            children.append(child)
            return child
        with mock.patch("openclean.project_artifacts.subprocess.Popen", side_effect=verbose):
            result = assess_project_artifact(special / "vendor", special, IgnoreRules())
        self.assertIn("Git 索引跟踪", result.block_reason)
        self.assertTrue(all(child.poll() is not None for child in children))

    def test_git_index_changes_after_scan_invalidate_old_candidate(self) -> None:
        self.init(self.project)
        tracked = self.write(self.project / "vendor/a.txt")
        item, = self.scan().items
        self.assertTrue(item.actionable)
        self.git(self.project, "add", "--", str(tracked))
        report = execute_cleanup([item], IgnoreRules(), home=self.home)
        self.assertEqual(report.outcomes[0].status, "blocked")
        self.assertFalse(self.trash.exists())

    def test_git_metadata_symlinks_and_cloud_index_are_not_queried(self) -> None:
        self.init(self.project)
        self.write(self.project / "target/output.bin")
        index = self.project / ".git/index"
        outside = self.write(self.home / "external-index")
        index.symlink_to(outside)
        with mock.patch("openclean.project_artifacts.subprocess.Popen") as process:
            result = self.scan()
        process.assert_not_called()
        self.assertFalse(result.complete)
        index.unlink()
        self.write(index)
        with mock.patch.object(FileFacts, "is_probable_cloud_placeholder", new=property(lambda f: f.path == index)), \
             mock.patch("openclean.project_artifacts.subprocess.Popen") as process:
            result = self.scan()
        process.assert_not_called()
        self.assertFalse(result.complete)

    def test_budgets_permission_and_cancel_are_not_clean_results(self) -> None:
        artifact = self.project / "target"
        self.write(artifact / "output.bin")
        for kwargs in ({"max_entries": 0}, {"timeout": 0}):
            assessment = assess_project_artifact(artifact, self.project, IgnoreRules(), **kwargs)
            self.assertFalse(assessment.complete)
            self.assertTrue(assessment.block_reason)
        with mock.patch("openclean.path_inspection.scandir_entries", side_effect=PermissionError("denied")):
            assessment = assess_project_artifact(artifact, self.project, IgnoreRules())
        self.assertFalse(assessment.complete)
        control = Control()
        control.cancel()
        with self.assertRaises(Cancelled):
            assess_project_artifact(artifact, self.project, IgnoreRules(), checkpoint=control.checkpoint)

    def test_protected_or_cloud_content_is_not_enumerated_and_symlinks_not_followed(self) -> None:
        artifact = self.project / "target"
        self.write(artifact / "output.bin")
        outside = self.home / "outside"
        self.write(outside / "demo-keypair.json")
        (artifact / "link").symlink_to(outside, target_is_directory=True)
        self.assertFalse(assess_project_artifact(artifact, self.project, IgnoreRules()).block_reason)
        knowledge = KnowledgeBase.from_mapping({"schema_version": 1, "protect": {"paths": [str(artifact)]}})
        with mock.patch("openclean.path_inspection.scandir_entries") as enumeration:
            assessment = assess_project_artifact(artifact, self.project, IgnoreRules(knowledge_base=knowledge))
        enumeration.assert_not_called()
        self.assertFalse(assessment.complete)
        with mock.patch.object(FileFacts, "is_probable_cloud_placeholder", new=property(lambda f: f.path == artifact)), \
             mock.patch("openclean.path_inspection.scandir_entries") as enumeration:
            assessment = assess_project_artifact(artifact, self.project, IgnoreRules())
        enumeration.assert_not_called()
        self.assertFalse(assessment.complete)

    def test_content_check_refuses_cross_filesystem_directory(self) -> None:
        artifact = self.project / "target"
        nested = artifact / "mounted"
        self.write(nested / "output.bin")
        with mock.patch("openclean.path_inspection.filesystem_id_retry",
                        side_effect=lambda path: 2 if path == nested else 1):
            result = assess_project_artifact(artifact, self.project, IgnoreRules())
        self.assertFalse(result.complete)
        self.assertIn("跨越文件系统", result.block_reason)

    def test_zig_requires_adjacent_regular_build_file_and_rechecks_it(self) -> None:
        self.write(self.project / ".zig-cache/output.bin")
        self.write(self.project / "zig-out/program")
        self.assertEqual(self.scan().items, [])
        marker = self.project / "build.zig"
        marker.write_text("// synthetic build\n")
        items = self.scan().items
        self.assertEqual({i.artifact_name for i in items}, {".zig-cache", "zig-out"})
        self.assertTrue(all(i.actionable and i.preselected for i in items))
        marker.unlink()
        report = execute_cleanup(items, IgnoreRules(), home=self.home)
        self.assertTrue(all(o.status == "blocked" for o in report.outcomes))
        marker.symlink_to(self.project / "package.json")
        self.assertEqual(self.scan().items, [])

    def test_cancelled_content_check_never_publishes_unchecked_candidate(self) -> None:
        self.write(self.project / "target/output.bin")
        with mock.patch("openclean.engine.assess_project_artifact", side_effect=Cancelled()):
            result = self.scan()
        self.assertTrue(result.cancelled)
        self.assertEqual(result.items, [])

    def test_analyze_does_not_inherit_purge_only_authored_content_policy(self) -> None:
        artifact = self.project / "target"
        self.write(artifact / "demo-keypair.json")
        item = next(e.item for e in analyze_path(self.project).entries if e.item.path == artifact)
        self.assertTrue(item.actionable)
        self.assertEqual(item.safety, "critical")
        self.assertFalse(item.preselected)


if __name__ == "__main__":
    unittest.main()
