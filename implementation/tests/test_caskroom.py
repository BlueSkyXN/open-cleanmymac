from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from openclean.analyzer import analyze_path
from openclean import caskroom, storage_diagnostics
from openclean.caskroom import assess_cask_version, caskroom_roots, scan_caskroom_versions
from openclean.cleanup import execute_cleanup, select_cleanup_items
from openclean.cleanup_guards import CleanupGuardContext
from openclean.cli import main
from openclean.engine import Cancelled, IgnoreRules, _apply_scan_guards, scan_domains
from openclean.models import FileFacts, FileIdentity
from openclean.processes import (
    OpenFileDetectionError,
    OpenFileSnapshot,
    ProcessDetectionError,
    ProcessSnapshot,
)
from openclean.scanpoints import DEVELOPER_JUNK

IDLE = (ProcessSnapshot(()), OpenFileSnapshot(()))
_TIMESTAMP = "20261016000000.000"


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def _make_cask(root: Path, token: str, receipt_version: str, *versions: str) -> Path:
    token_root = root / token
    casks = token_root / ".metadata" / receipt_version / _TIMESTAMP / "Casks"
    casks.mkdir(parents=True)
    _write_json(token_root / ".metadata/INSTALL_RECEIPT.json", {
        "source": {"version": receipt_version},
        "uninstall_artifacts": [{"binary": ["bin/tool", {"target": token}]}],
    })
    _write_json(casks / f"{token}.json", {})
    for version in versions:
        (token_root / version / "bin").mkdir(parents=True)
        (token_root / version / "bin/tool").write_bytes(b"x" * 4096)
    binary = root.parent / "bin" / token
    binary.parent.mkdir(exist_ok=True)
    binary.symlink_to(token_root / receipt_version / "bin/tool")
    return token_root


class AssessCaskVersionTests(unittest.TestCase):
    def test_version_comparisons(self) -> None:
        cases = (
            ("0.162.1", "0.137.0", "older_version_residue", False),
            ("0.137.0", "0.137.0", "same_version_residue", True),
            ("0.137.0", "0.162.1", "pending_update", True),
            ("1.0.98-2", "1.0.98-2", "version_unknown", True),
            ("2.0", "1.0,2", "version_unknown", True),
            ("2", "2.0", "same_version_residue", True),
        )
        for installed, directory, status, blocked in cases:
            with self.subTest(installed=installed, directory=directory):
                state = assess_cask_version("fixture", installed, directory)
                self.assertEqual(state.status, status)
                self.assertEqual(state.blocks_cleanup, blocked)


class CaskroomFixture(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.home = self.base / "home"
        self.trash = self.home / ".Trash"
        self.trash.mkdir(parents=True, mode=0o700)
        self.root = self.base / "Caskroom"
        self.root.mkdir()
        self.patch(mock.patch.dict(os.environ, {
            "HOME": str(self.home), "HOMEBREW_PREFIX": str(self.base),
        }, clear=True))
        self.roots = self.patch(mock.patch("openclean.caskroom.caskroom_roots", return_value=(self.root,)))
        self.guard_roots = self.patch(mock.patch("openclean.cleanup_guards.caskroom_roots", return_value=(self.root,)))
        self.scan_processes = self.patch(mock.patch("openclean.caskroom.capture_process_snapshot", return_value=IDLE[0]))
        self.scan_handles = self.patch(mock.patch("openclean.caskroom.capture_open_file_snapshot", return_value=IDLE[1]))
        self.patch(mock.patch("openclean.engine.capture_process_snapshot", return_value=IDLE[0]))
        self.live_processes = self.patch(mock.patch("openclean.cleanup.capture_process_snapshot", return_value=IDLE[0]))
        self.live_handles = self.patch(mock.patch("openclean.cleanup.capture_open_file_snapshot", return_value=IDLE[1]))
        self.guard_handles = self.patch(mock.patch("openclean.cleanup_guards.capture_open_file_snapshot", return_value=IDLE[1]))

    def patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def _home_brew_fixture(self):
        """在 HOME 内搭建自定义 prefix 的完整 cask，返回 (brew, brew_root)。"""
        token = self.make_cask()
        brew = self.home / "brew"
        brew_root = brew / "Caskroom"
        if brew_root.exists():
            return brew, brew_root
        brew.mkdir(exist_ok=True)
        shutil.copytree(self.root, brew_root, symlinks=True)
        target = brew_root / token.name
        shutil.rmtree(target)
        shutil.copytree(token, target, symlinks=True)
        links = brew / "bin"
        links.mkdir(exist_ok=True)
        (links / "fixture").symlink_to(target / "2.0/bin/tool")
        self.roots.return_value = (brew_root,)
        self.guard_roots.return_value = (brew_root,)
        return brew, brew_root

    def make_cask(self) -> Path:
        return _make_cask(self.root, "fixture", "2.0", "1.0", "2.0")

    def scan(self, protection=None, **kwargs):
        return scan_caskroom_versions(
            protection or IgnoreRules(), roots=(self.root,), snapshots=kwargs.pop("snapshots", IDLE), **kwargs,
        )

    def old(self, result):
        return next(item for item in result.items if item.path == self.root / "fixture/1.0")

    def item(self):
        self.make_cask()
        item = self.old(self.scan())
        self.assertTrue(item.actionable, item.action_block_reason)
        return item

    def assert_blocked(self, protection=None, text=""):
        protection = protection or IgnoreRules()
        result = self.scan(protection)
        guards = CleanupGuardContext(protection, lambda: IDLE[0], home=self.home)
        result.items = [_apply_scan_guards(item, guards, result, lambda: None) for item in result.items]
        item = self.old(result)
        self.assertFalse(item.actionable)
        self.assertIn(text, item.action_block_reason)
        report = execute_cleanup([item], protection, home=self.home)
        self.assertEqual(report.outcomes[0].status, "blocked")
        self.assertTrue(item.path.exists())
        self.assertEqual(list(self.trash.iterdir()), [])
        return item


class ScanCaskroomTests(CaskroomFixture):
    def test_old_version_candidate_and_current_protection(self) -> None:
        token = self.make_cask()
        result = self.scan()
        self.assertEqual(len(result.items), 2)
        old = self.old(result)
        self.assertEqual(old.updater_status, "older_version_residue")
        self.assertTrue(old.actionable)
        self.assertEqual(old.safety, "critical")
        self.assertFalse(old.preselected)
        self.assertTrue(old.requires_explicit_selection)
        self.assertIsNotNone(old.identity)
        self.assertEqual(old.cleanup_root_identity, FileIdentity.from_stat(token.stat()))
        self.assertGreater(old.size, 0)
        current = next(item for item in result.items if item.path == token / "2.0")
        self.assertFalse(current.actionable)
        self.assertIn("当前安装版本", current.action_block_reason)
        self.assertEqual(select_cleanup_items(result.items, include_critical=True), [])
        self.assertEqual(select_cleanup_items(result.items, selectors=(str(old.path),), include_critical=True), [old])

    def test_single_version_not_reported(self) -> None:
        _make_cask(self.root, "fixture", "2.0", "2.0")
        self.assertEqual(self.scan().items, [])

    def test_missing_receipt_blocks(self) -> None:
        token = self.make_cask()
        (token / ".metadata/INSTALL_RECEIPT.json").unlink()
        self.assert_blocked(text="安装状态")

    def test_brew_running_blocks(self) -> None:
        self.make_cask()
        processes = ProcessSnapshot(("/bin/bash -p /opt/homebrew/Library/Homebrew/brew.sh update",))
        old = self.old(self.scan(snapshots=(processes, IDLE[1])))
        self.assertFalse(old.actionable)
        self.assertIn("Homebrew 正在运行", old.action_block_reason)

    def test_unknown_snapshots_block(self) -> None:
        self.make_cask()
        for snapshots, message in (((None, IDLE[1]), "进程状态未知"), ((IDLE[0], None), "打开句柄状态未知")):
            with self.subTest(message=message):
                old = self.old(self.scan(snapshots=snapshots))
                self.assertFalse(old.actionable)
                self.assertIn(message, old.action_block_reason)

    def test_failed_snapshot_capture_blocks(self) -> None:
        self.make_cask()
        self.scan_processes.side_effect = ProcessDetectionError("unavailable")
        self.scan_handles.side_effect = OpenFileDetectionError("unavailable")
        result = self.scan(snapshots=None)
        self.assertFalse(self.old(result).actionable)
        self.assertEqual({issue.code for issue in result.issues}, {"process_detection_failed", "open_file_detection_failed"})

    def test_open_handles_block(self) -> None:
        token = self.make_cask()
        snapshot = OpenFileSnapshot((str(token / "1.0/bin/tool"),))
        old = self.old(self.scan(snapshots=(IDLE[0], snapshot)))
        self.assertFalse(old.actionable)
        self.assertIn("存在打开句柄", old.action_block_reason)

    def test_symlink_version_directory_excluded(self) -> None:
        token = _make_cask(self.root, "fixture", "2.0", "2.0")
        outside = self.base / "outside"
        outside.mkdir()
        (token / "1.0").symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.scan().items, [])

    def test_ignored_candidate_excluded(self) -> None:
        token = self.make_cask()
        result = self.scan(IgnoreRules((str(token / "1.0"),)))
        self.assertFalse(any(item.path == token / "1.0" for item in result.items))

    def test_ignored_root_is_not_enumerated(self) -> None:
        self.make_cask()
        with mock.patch("openclean.caskroom.os.scandir", side_effect=AssertionError("must not enumerate")):
            self.assertEqual(self.scan(IgnoreRules((str(self.root),))).items, [])

    def test_symlink_ancestor_is_not_enumerated(self) -> None:
        self.make_cask()
        alias = self.base / "alias"
        alias.symlink_to(self.base, target_is_directory=True)
        with mock.patch("openclean.caskroom.os.scandir", side_effect=AssertionError("must not enumerate")):
            result = scan_caskroom_versions(IgnoreRules(), roots=(alias / "Caskroom",), snapshots=IDLE)
        self.assertEqual(result.items, [])
        self.assertFalse(result.complete)

    def test_cancel_returns_incomplete_result(self) -> None:
        self.make_cask()
        result = self.scan(checkpoint=mock.Mock(side_effect=Cancelled()))
        self.assertTrue(result.cancelled)
        self.assertFalse(result.complete)

    def test_cancel_before_handle_snapshot_returns_incomplete_result(self) -> None:
        def checkpoint():
            raise Cancelled()

        result = self.scan(snapshots=None, checkpoint=checkpoint)
        self.assertTrue(result.cancelled)
        self.assertFalse(result.complete)
        self.scan_handles.assert_not_called()

    def test_explicit_roots_never_scan_other_default_root(self) -> None:
        self.make_cask()
        foreign_root = self.base / "foreign/Caskroom"
        foreign = _make_cask(foreign_root, "fixture", "2.0", "1.0", "2.0")
        self.roots.return_value = (foreign_root, self.root)
        item = self.old(self.scan())
        report = execute_cleanup([item], IgnoreRules(), home=self.home)
        self.assertEqual(report.outcomes[0].item.path, self.root / "fixture/1.0")
        self.assertEqual(report.outcomes[0].destination, self.trash / "1.0")
        self.assertTrue((foreign / "1.0/bin/tool").exists())
        self.assertFalse(item.path.exists())

    def test_dynamic_developer_scan_and_cli_preview(self) -> None:
        token = self.make_cask()
        point = next(p for p in DEVELOPER_JUNK if p.scanner == "caskroom-versions")
        with mock.patch.dict("openclean.engine.DOMAINS", {"developer": [point]}):
            result = scan_domains(["developer"], workers=1)
            self.assertTrue(self.old(result).actionable)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = main(["clean", "dev", "--json", "--include-critical", "--select", str(token / "1.0")])
        self.assertEqual(status, 0)
        json.loads(output.getvalue())
        self.assertTrue((token / "1.0").exists())
        self.assertEqual(list(self.trash.iterdir()), [])


class CaskroomMetadataTests(CaskroomFixture):
    def test_metadata_symlink_blocks(self) -> None:
        token = self.make_cask()
        external = self.base / "metadata"
        (token / ".metadata").rename(external)
        (token / ".metadata").symlink_to(external, target_is_directory=True)
        self.assert_blocked(text="符号链接")

    def test_receipt_symlink_blocks(self) -> None:
        token = self.make_cask()
        receipt = token / ".metadata/INSTALL_RECEIPT.json"
        external = self.base / "receipt.json"
        receipt.rename(external)
        receipt.symlink_to(external)
        self.assert_blocked(text="符号链接")

    def test_ignored_metadata_not_read(self) -> None:
        token = self.make_cask()
        with mock.patch("openclean.caskroom.os.read", side_effect=AssertionError("must not read")):
            self.assert_blocked(IgnoreRules((str(token / ".metadata"),)), text="忽略")

    def test_ignored_receipt_not_read(self) -> None:
        token = self.make_cask()
        with mock.patch("openclean.caskroom.os.read", side_effect=AssertionError("must not read")):
            self.assert_blocked(IgnoreRules((str(token / ".metadata/INSTALL_RECEIPT.json"),)), text="忽略")

    def test_cloud_receipt_not_read(self) -> None:
        token = self.make_cask()
        receipt = token / ".metadata/INSTALL_RECEIPT.json"
        original = FileFacts.is_probable_cloud_placeholder.fget
        with mock.patch.object(FileFacts, "is_probable_cloud_placeholder", new=property(
            lambda facts: facts.path == receipt or original(facts)
        )), mock.patch("openclean.caskroom.os.read", side_effect=AssertionError("must not read")):
            self.assert_blocked(text="云占位")

    def test_invalid_json_and_version_block(self) -> None:
        token = self.make_cask()
        receipt = token / ".metadata/INSTALL_RECEIPT.json"
        for content in ("{", "[]", '{"source": {"version": "2.0-1"}}', '{"source": {"version": "../2.0"}}'):
            with self.subTest(content=content):
                receipt.write_text(content)
                self.assert_blocked()

    def test_oversized_receipt_blocks(self) -> None:
        token = self.make_cask()
        (token / ".metadata/INSTALL_RECEIPT.json").write_bytes(b"x" * (caskroom._MAX_RECEIPT_BYTES + 1))
        self.assert_blocked(text="大小上限")

    def test_missing_timestamped_metadata_blocks(self) -> None:
        token = self.make_cask()
        shutil.rmtree(token / ".metadata/2.0")
        self.assert_blocked(text="带时间戳")

    def test_newest_metadata_must_agree_with_receipt(self) -> None:
        token = self.make_cask()
        casks = token / ".metadata/1.0/20261017000000.000/Casks"
        casks.mkdir(parents=True)
        _write_json(casks / "fixture.json", {})
        self.assert_blocked(text="版本不一致")

    def test_same_timestamp_different_versions_blocks(self) -> None:
        token = self.make_cask()
        casks = token / ".metadata/1.0" / _TIMESTAMP / "Casks"
        casks.mkdir(parents=True)
        _write_json(casks / "fixture.json", {})
        self.assert_blocked(text="不唯一")

    def test_metadata_budget_exhaustion_blocks(self) -> None:
        self.make_cask()
        with mock.patch("openclean.caskroom._MAX_METADATA_ENTRIES", 32):
            result = self.scan()
        self.assertFalse(any(item.actionable for item in result.items))

    def test_missing_current_directory_blocks(self) -> None:
        token = self.make_cask()
        (token / "2.0").rename(token / "1.5")
        self.assert_blocked()

    def test_installed_definition_version_conflict_blocks(self) -> None:
        token = self.make_cask()
        _write_json(token / ".metadata/2.0" / _TIMESTAMP / "Casks/fixture.json", {"version": "1.0"})
        self.assert_blocked(text="版本不一致")

    def test_installed_definition_symlink_blocks(self) -> None:
        token = self.make_cask()
        definition = token / ".metadata/2.0" / _TIMESTAMP / "Casks/fixture.json"
        external = self.base / "definition.json"
        definition.rename(external)
        definition.symlink_to(external)
        self.assert_blocked(text="符号链接")

    def test_legacy_ruby_is_not_executed(self) -> None:
        token = self.make_cask()
        definition = token / ".metadata/2.0" / _TIMESTAMP / "Casks/fixture.json"
        definition.unlink()
        definition.with_suffix(".rb").write_text('raise "must not execute"\n')
        self.assertTrue(self.old(self.scan()).actionable)

    def test_active_binary_link_to_old_version_blocks(self) -> None:
        token = self.make_cask()
        link = self.base / "bin/fixture"
        link.unlink()
        link.symlink_to(token / "1.0/bin/tool")
        self.assert_blocked(text="仍指向旧版本")
        self.assertTrue(link.exists())

    def test_relative_current_binary_link_is_supported(self) -> None:
        self.make_cask()
        link = self.base / "bin/fixture"
        link.unlink()
        link.symlink_to("../Caskroom/fixture/2.0/bin/tool")
        self.assertTrue(self.old(self.scan()).actionable)

    def test_current_source_symlink_blocks(self) -> None:
        token = self.make_cask()
        current = token / "2.0/bin/tool"
        current.unlink()
        current.symlink_to(token / "1.0/bin/tool")
        self.assert_blocked(text="符号链接")

    def test_missing_binary_link_blocks(self) -> None:
        self.make_cask()
        (self.base / "bin/fixture").unlink()
        self.assert_blocked()

    def test_missing_or_unsupported_artifacts_remain_report_only(self) -> None:
        token = self.make_cask()
        receipt = token / ".metadata/INSTALL_RECEIPT.json"
        for artifacts in (None, [], [{"app": ["Fixture.app"]}], [{"binary": ["../tool"]}]):
            with self.subTest(artifacts=artifacts):
                _write_json(receipt, {"source": {"version": "2.0"}, "uninstall_artifacts": artifacts})
                self.assert_blocked()

    def test_definition_and_receipt_artifact_conflict_blocks(self) -> None:
        token = self.make_cask()
        _write_json(token / ".metadata/2.0" / _TIMESTAMP / "Casks/fixture.json", {
            "artifacts": [{"binary": ["other-tool"]}],
        })
        self.assert_blocked(text="安装入口不一致")

    def test_supported_link_artifacts_use_homebrew_target_names(self) -> None:
        token = self.make_cask()
        cases = (
            ("binary", "tool", "bin/tool"),
            ("manpage", "tool.1", "share/man/man1/tool.1"),
            ("manpage", "tool.5.gz", "share/man/man5/tool.5.gz"),
            ("bash_completion", "tool.bash", "etc/bash_completion.d/tool"),
            ("zsh_completion", "tool.zsh", "share/zsh/site-functions/_tool"),
            ("zsh_completion", "_tool", "share/zsh/site-functions/_tool"),
            ("fish_completion", "tool.sh", "share/fish/vendor_completions.d/tool.fish"),
            ("pwsh_completion", "tool.ps1", "share/pwsh/completions/_tool.ps1"),
            ("pwsh_completion", "_tool.ps1", "share/pwsh/completions/_tool.ps1"),
        )
        receipt = token / ".metadata/INSTALL_RECEIPT.json"
        for kind, filename, target in cases:
            with self.subTest(kind=kind, filename=filename):
                source = token / "2.0" / filename
                source.write_bytes(b"fixture\n")
                link = self.base / target
                link.parent.mkdir(parents=True, exist_ok=True)
                if link.is_symlink():
                    link.unlink()
                link.symlink_to(source)
                _write_json(receipt, {"source": {"version": "2.0"}, "uninstall_artifacts": [{kind: [filename]}]})
                self.assertTrue(self.old(self.scan()).actionable)
                link.unlink()
                link.symlink_to(token / "1.0/bin/tool")
                self.assert_blocked(text="仍指向旧版本")
                # 指向旧版的残留链接会阻断后续全量扫描，断言后立即清理。
                link.unlink()

    def test_only_path_and_absolute_binary_target_are_supported(self) -> None:
        token = self.make_cask()
        nested = token / "2.0/package/bin/tool"
        nested.parent.mkdir(parents=True)
        nested.write_bytes(b"fixture\n")
        target = self.base / "custom/tool"
        target.parent.mkdir()
        target.symlink_to(nested)
        _write_json(token / ".metadata/INSTALL_RECEIPT.json", {
            "source": {"version": "2.0"},
            "uninstall_artifacts": [{"binary": ["bin/tool", {"target": str(target)}]}],
        })
        _write_json(token / ".metadata/2.0" / _TIMESTAMP / "Casks/fixture.json", {
            "url_specs": {"only_path": "package"},
        })
        self.assertTrue(self.old(self.scan()).actionable)

    def test_generated_completions_and_zap_preserve_cli_cask_support(self) -> None:
        token = self.make_cask()
        receipt = token / ".metadata/INSTALL_RECEIPT.json"
        data = json.loads(receipt.read_text())
        data["uninstall_artifacts"].extend([
            {"generate_completions_from_executable": [
                "bin/tool", "completion", {"base_name": "fixture", "shell_parameter_format": None, "shells": ["bash", "zsh", "fish"]},
            ]},
            {"zap": [{"rmdir": "~/.fixture"}]},
        ])
        _write_json(receipt, data)
        completions = ("etc/bash_completion.d/fixture", "share/zsh/site-functions/_fixture", "share/fish/vendor_completions.d/fixture.fish")
        for relative in completions:
            path = self.base / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic completion\n")
        with mock.patch("subprocess.run", side_effect=AssertionError("must not run cask commands")):
            result = self.scan()
        item = self.old(result)
        self.assertTrue(item.actionable, item.action_block_reason)
        report = execute_cleanup([item], IgnoreRules(), home=self.home)
        self.assertTrue(report.complete)
        self.assertTrue(all((self.base / relative).is_file() for relative in completions))
        self.assertTrue((self.base / "bin/fixture").exists())

    def test_generated_completion_link_to_old_version_blocks(self) -> None:
        token = self.make_cask()
        receipt = token / ".metadata/INSTALL_RECEIPT.json"
        data = json.loads(receipt.read_text())
        data["uninstall_artifacts"].append({"generate_completions_from_executable": [
            "bin/tool", "completion", {"base_name": "fixture", "shells": ["bash"]},
        ]})
        _write_json(receipt, data)
        target = self.base / "etc/bash_completion.d/fixture"
        target.parent.mkdir(parents=True)
        target.symlink_to(token / "1.0/bin/tool")
        self.assert_blocked(text="符号链接")
        target.unlink()

    def test_zap_alone_is_not_installation_evidence(self) -> None:
        token = self.make_cask()
        _write_json(token / ".metadata/INSTALL_RECEIPT.json", {
            "source": {"version": "2.0"}, "uninstall_artifacts": [{"zap": [{"rmdir": "~/.fixture"}]}],
        })
        self.assert_blocked(text="安装链接")

    def test_uninstall_flight_blocks_remain_report_only(self) -> None:
        token = self.make_cask()
        receipt = token / ".metadata/INSTALL_RECEIPT.json"
        data = json.loads(receipt.read_text())
        data["uninstall_flight_blocks"] = True
        _write_json(receipt, data)
        self.assert_blocked(text="脚本钩子")

    def test_receipt_changed_to_link_between_probe_and_open_blocks(self) -> None:
        token = self.make_cask()
        receipt = token / ".metadata/INSTALL_RECEIPT.json"
        external = self.base / "external-receipt.json"
        external.write_bytes(receipt.read_bytes())
        original = caskroom.open_resource
        changed = False
        def replace_receipt(facts, parent_fd):
            nonlocal changed
            if facts.path == receipt and not changed:
                receipt.unlink()
                receipt.symlink_to(external)
                changed = True
            return original(facts, parent_fd)
        with mock.patch("openclean.caskroom.open_resource", side_effect=replace_receipt):
            self.assert_blocked()

    def test_receipt_change_during_installation_assessment_blocks(self) -> None:
        token = self.make_cask()
        receipt = token / ".metadata/INSTALL_RECEIPT.json"
        original = caskroom._check_installed_links
        def change_receipt(*args):
            original(*args)
            data = json.loads(receipt.read_text())
            data["source"]["version"] = "3.0"
            _write_json(receipt, data)
        with mock.patch("openclean.caskroom._check_installed_links", side_effect=change_receipt):
            self.assert_blocked(text="复核期间变化")

    def test_metadata_cross_filesystem_blocks(self) -> None:
        token = self.make_cask()
        original = caskroom.filesystem_id_retry
        metadata = token / ".metadata"
        def filesystem(path):
            return -1 if path.is_relative_to(metadata) else original(path)
        with mock.patch("openclean.caskroom.filesystem_id_retry", side_effect=filesystem):
            self.assert_blocked(text="文件系统")

    def test_metadata_ancestor_cross_filesystem_blocks(self) -> None:
        token = self.make_cask()
        original = caskroom.filesystem_id_retry
        metadata = token / ".metadata"
        def filesystem(path):
            return -1 if path == metadata else original(path)
        with mock.patch("openclean.caskroom.filesystem_id_retry", side_effect=filesystem):
            self.assert_blocked(text="文件系统")

    def test_installed_metadata_ignored_after_receipt_read_blocks(self) -> None:
        token = self.make_cask()
        self.assert_blocked(IgnoreRules((str(token / ".metadata/2.0"),)), text="忽略")

    def test_internal_json_definition_is_supported(self) -> None:
        token = self.make_cask()
        definition = token / ".metadata/2.0" / _TIMESTAMP / "Casks/fixture.json"
        definition.rename(definition.with_name("fixture.internal.json"))
        self.assertTrue(self.old(self.scan()).actionable)

    def test_dotdot_link_through_symlink_component_blocks(self) -> None:
        # 词法折叠 .. 会把 2.0/bridge/../bin/tool 当作 2.0/bin/tool，
        # 实际解析穿过符号链接后仍指向 1.0；必须判为指向旧版本。
        token = self.make_cask()
        (token / "1.0/nested").mkdir()
        (token / "2.0/bridge").symlink_to(token / "1.0/nested", target_is_directory=True)
        link = self.base / "bin/fixture"
        link.unlink()
        link.symlink_to(token / "2.0/bridge/../bin/tool")
        self.assert_blocked(text="符号链接组件")
        self.assertTrue(link.is_symlink())

    def test_dotdot_link_without_symlinks_still_allowed(self) -> None:
        token = self.make_cask()
        link = self.base / "bin/fixture"
        link.unlink()
        link.symlink_to(token / "2.0/bin/../bin/tool")
        self.assertTrue(self.old(self.scan()).actionable)

    def test_legacy_declared_entry_still_linked_to_old_blocks(self) -> None:
        # 旧版本元数据记录过的入口在升级改名后仍可能保留，全量扫描不得放行。
        token = self.make_cask()
        legacy = self.base / "bin/fixture-legacy"
        legacy.symlink_to(token / "1.0/bin/tool")
        self.assert_blocked(text="旧版本")
        self.assertTrue(legacy.is_symlink())

    def test_unrelated_prefix_link_outside_token_is_ignored(self) -> None:
        self.make_cask()
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "tool").write_bytes(b"x")
        other = self.base / "bin/unrelated-tool"
        other.symlink_to(elsewhere / "tool")
        self.assertTrue(self.old(self.scan()).actionable)

    def test_residual_terminal_symlink_to_old_blocks_scan_and_execution(self) -> None:
        token = self.make_cask()
        item = self.old(self.scan())
        (token / "2.0/bridge").symlink_to(token / "1.0/bin/tool")
        legacy = self.base / "bin/legacy"
        legacy.symlink_to(token / "2.0/bridge")
        self.assert_blocked(text="旧版本")
        report = execute_cleanup([item], IgnoreRules(), home=self.home)
        self.assertEqual(report.outcomes[0].status, "blocked")
        self.assertEqual(legacy.resolve(strict=True), token / "1.0/bin/tool")

    def test_historical_absolute_target_chain_to_old_blocks(self) -> None:
        token = self.make_cask()
        definition = token / ".metadata/1.0/20250101000000.000/Casks/fixture.json"
        definition.parent.mkdir(parents=True)
        legacy = self.home / "custom/legacy"
        legacy.parent.mkdir()
        bridge = self.home / "custom/bridge"
        bridge.symlink_to(token / "1.0/bin/tool")
        legacy.symlink_to("bridge")
        _write_json(definition, {"version": "1.0", "artifacts": [
            {"binary": ["bin/tool", {"target": str(legacy)}]},
        ]})
        self.assert_blocked(text="旧版本")
        self.assertEqual(legacy.resolve(strict=True), token / "1.0/bin/tool")

    def test_unrelated_terminal_link_chain_is_not_a_cask_blocker(self) -> None:
        self.make_cask()
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "tool").write_bytes(b"x")
        (elsewhere / "alias").symlink_to("tool")
        (self.base / "bin/unrelated-tool").symlink_to(elsewhere / "alias")
        self.assertTrue(self.old(self.scan()).actionable)

    def test_unrelated_homebrew_opt_link_does_not_block_cleanup(self) -> None:
        token = self.make_cask()
        other = self.base / "Cellar/other/1.0"
        (other / "bin").mkdir(parents=True)
        (other / "bin/other").write_bytes(b"x")
        (self.base / "opt").mkdir()
        (self.base / "opt/other").symlink_to(other, target_is_directory=True)
        unrelated = self.base / "bin/unrelated"
        unrelated.symlink_to("../opt/other/bin/other")
        item = self.old(self.scan())
        self.assertTrue(item.actionable, item.action_block_reason)
        report = execute_cleanup([item], IgnoreRules(), home=self.home)
        self.assertEqual(report.outcomes[0].status, "moved_to_trash")
        self.assertTrue(unrelated.exists())
        self.assertTrue((token / "2.0/bin/tool").exists())

    def test_opt_link_back_to_old_version_blocks(self) -> None:
        token = self.make_cask()
        (self.base / "opt").mkdir()
        (self.base / "opt/legacy").symlink_to(token / "1.0", target_is_directory=True)
        (self.base / "bin/legacy").symlink_to("../opt/legacy/bin/tool")
        self.assert_blocked(text="旧版本")

    def test_residual_dotdot_uses_resolved_directory(self) -> None:
        token = self.make_cask()
        (token / "1.0/nested").mkdir()
        (token / "2.0/bridge").symlink_to(token / "1.0/nested", target_is_directory=True)
        (self.base / "bin/legacy").symlink_to(token / "2.0/bridge/../bin/tool")
        self.assert_blocked(text="旧版本")

    def test_unrelated_dangling_opt_link_does_not_block(self) -> None:
        self.make_cask()
        (self.base / "opt").mkdir()
        (self.base / "opt/missing").symlink_to("../Cellar/missing/1.0", target_is_directory=True)
        (self.base / "bin/unrelated").symlink_to("../opt/missing/bin/tool")
        self.assertTrue(self.old(self.scan()).actionable)

    def test_large_manpage_inventory_reuses_component_checks(self) -> None:
        self.make_cask()
        pages = self.base / "share/man/man3"
        pages.mkdir(parents=True)
        target = self.base / "Cellar/other/1.0/share/man/man3/topic.3"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"manual")
        for index in range(6000):
            (pages / f"alias{index:04d}.3").symlink_to(
                "../../../Cellar/other/1.0/share/man/man3/topic.3",
            )
        item = self.old(self.scan())
        self.assertTrue(item.actionable, item.action_block_reason)

    def test_protected_intermediate_link_reports_entry_and_component(self) -> None:
        token = self.make_cask()
        (self.base / "opt").mkdir()
        bridge = self.base / "opt/legacy"
        bridge.symlink_to(token / "1.0", target_is_directory=True)
        entry = self.base / "bin/legacy"
        entry.symlink_to("../opt/legacy/bin/tool")
        item = self.assert_blocked(IgnoreRules((str(bridge),)), text="忽略")
        self.assertIn(str(entry), item.action_block_reason)
        self.assertIn(str(bridge), item.action_block_reason)

    def test_inventory_cache_rechecks_links_after_resolution(self) -> None:
        token = self.make_cask()
        outside = self.base / "outside"
        outside.write_bytes(b"x")
        entry = self.base / "bin/unrelated"
        entry.symlink_to(outside)
        original = caskroom._resolve_inventory_link
        changed = False

        def resolve(inspection, path, inventory):
            nonlocal changed
            target = original(inspection, path, inventory)
            if path == entry and not changed:
                changed = True
                entry.unlink()
                entry.symlink_to(token / "1.0/bin/tool")
            return target

        with mock.patch("openclean.caskroom._resolve_inventory_link", side_effect=resolve):
            self.assert_blocked(text="变化")
        self.assertTrue(changed)

    def test_intermediate_link_changed_during_resolution_blocks(self) -> None:
        token = self.make_cask()
        (self.base / "opt").mkdir()
        bridge = self.base / "opt/legacy"
        bridge.symlink_to(token / "2.0", target_is_directory=True)
        (self.base / "bin/legacy").symlink_to("../opt/legacy/bin/tool")
        original = os.readlink
        changed = False

        def readlink(path, **kwargs):
            nonlocal changed
            value = original(path, **kwargs)
            if not changed and value == str(token / "2.0"):
                changed = True
                bridge.unlink()
                bridge.symlink_to(token / "1.0", target_is_directory=True)
            return value

        with mock.patch("openclean.caskroom.os.readlink", side_effect=readlink):
            self.assert_blocked(text="变化")
        self.assertTrue(changed)

    def test_terminal_link_cycle_and_protected_target_block(self) -> None:
        token = self.make_cask()
        bridge = token / "2.0/bridge"
        bridge.symlink_to("bridge")
        (self.base / "bin/legacy").symlink_to(bridge)
        self.assert_blocked(text="循环")
        bridge.unlink()
        bridge.symlink_to(token / "1.0/bin/tool")
        self.assert_blocked(IgnoreRules((str(bridge),)), text="忽略")

    def test_terminal_link_hop_limit_blocks(self) -> None:
        token = self.make_cask()
        target = token / "1.0/bin/tool"
        for index in range(caskroom._MAX_LINK_HOPS + 1):
            link = token / "2.0" / f"bridge{index}"
            link.symlink_to(target)
            target = link
        (self.base / "bin/legacy").symlink_to(target)
        self.assert_blocked(text="跳数上限")

    def test_current_installed_entry_still_requires_a_direct_link(self) -> None:
        token = self.make_cask()
        (token / "2.0/bridge").symlink_to("bin/tool")
        entry = self.base / "bin/fixture"
        entry.unlink()
        entry.symlink_to(token / "2.0/bridge")
        self.assert_blocked(text="符号链接")

    def test_ignored_root_reports_clean_skip(self) -> None:
        self.make_cask()
        result = self.scan(IgnoreRules((str(self.root),)))
        self.assertEqual(result.items, [])
        self.assertEqual(result.issues, [])
        self.assertTrue(result.complete)

    def test_cancel_during_measurement_stops_traversal(self) -> None:
        token = self.make_cask()
        old = token / "1.0"
        for index in range(120):
            (old / "bin" / f"payload-{index}").write_bytes(b"x" * 32)
        visits = 0
        cancelled = False
        original = storage_diagnostics.lstat_retry
        from openclean.engine import Cancelled as EngineCancelled

        def lstat(path):
            nonlocal visits, cancelled
            if cancelled and path.is_relative_to(old):
                visits += 1
            return original(path)

        original_scandir = storage_diagnostics.scandir_entries

        def entries(path):
            nonlocal cancelled
            if path == old / "bin":
                cancelled = True
            return original_scandir(path)

        def checkpoint():
            if cancelled:
                raise EngineCancelled()

        with mock.patch("openclean.storage_diagnostics.scandir_entries", side_effect=entries), \
             mock.patch("openclean.storage_diagnostics.lstat_retry", side_effect=lstat):
            result = self.scan(checkpoint=checkpoint)
        self.assertTrue(result.cancelled)
        self.assertLess(visits, 20)

    def test_many_tokens_scale_and_cross_token_residue(self) -> None:
        # 真实规模的 prefix 链接扫描必须一次解析跨 token 复用，
        # 且一个 token 的残留链接不能让其它 token 全部阻断。
        tokens = []
        for index in range(24):
            tokens.append(_make_cask(self.root, f"tool{index:02d}", "2.0", "1.0", "2.0"))
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "tool").write_bytes(b"x")
        for index in range(12):
            (self.base / "bin" / f"unrelated{index}").symlink_to(elsewhere / "tool")
        with mock.patch("openclean.caskroom._iter_prefix_links", wraps=caskroom._iter_prefix_links) as enumerate_links:
            result = self.scan()
        self.assertEqual(enumerate_links.call_count, 1)
        olds = [item for item in result.items if item.path.name == "1.0"]
        self.assertEqual(len(olds), 24)
        self.assertTrue(all(item.actionable for item in olds),
                        {item.path.parent.name: item.action_block_reason for item in olds})
        # 指向任何 token 旧版本的残留链接阻断该 token 的清理。
        (self.base / "bin/tool03-legacy").symlink_to(tokens[3] / "1.0/bin/tool")
        blocked = self.scan()
        state = {item.path.parent.name: item.actionable for item in blocked.items if item.path.name == "1.0"}
        self.assertFalse(state["tool03"])
        self.assertTrue(sum(1 for ok in state.values() if ok) >= 22)

    def test_domain_scan_reuses_cask_checks_but_next_scan_is_fresh(self) -> None:
        tokens = [_make_cask(self.root, f"tool{index:02d}", "2.0", "1.0", "2.0")
                  for index in range(24)]
        point = next(p for p in DEVELOPER_JUNK if p.scanner == "caskroom-versions")
        with mock.patch.dict("openclean.engine.DOMAINS", {"developer": [point]}), \
             mock.patch("openclean.caskroom._iter_prefix_links", wraps=caskroom._iter_prefix_links) as inventory, \
             mock.patch("openclean.caskroom._installation_version", wraps=caskroom._installation_version) as installation:
            result = scan_domains(["developer"], workers=1)
            self.assertEqual(sum(item.actionable for item in result.items), 24)
            self.assertEqual(inventory.call_count, 1)
            self.assertEqual(installation.call_count, 24)
            self.assertEqual(self.scan_handles.call_count, 1)
            self.assertEqual(self.guard_handles.call_count, 0)
            (self.base / "bin/legacy").symlink_to(tokens[3] / "1.0/bin/tool")
            fresh = scan_domains(["developer"], workers=1)
            self.assertEqual(inventory.call_count, 2)
            self.assertEqual(self.scan_handles.call_count, 2)
            state = {item.path.parent.name: item.actionable
                     for item in fresh.items if item.path.name == "1.0"}
            self.assertFalse(state["tool03"])
            self.assertTrue(state["tool02"])

    def test_analyze_reuses_checks_and_failed_handle_snapshot(self) -> None:
        _, root = self._home_brew_fixture()
        old = root / "fixture/1.0"
        for index in range(8):
            (old / f"file{index}").write_bytes(b"x")
        self.scan_handles.side_effect = OpenFileDetectionError("unavailable")
        self.guard_handles.side_effect = OpenFileDetectionError("unavailable")
        with mock.patch("openclean.caskroom._installation_version", wraps=caskroom._installation_version) as installation:
            result = analyze_path(old, workers=4)
        self.assertEqual(installation.call_count, 1)
        self.assertEqual(self.scan_handles.call_count, 1)
        self.assertEqual(self.guard_handles.call_count, 0)
        self.assertTrue(result.entries)
        self.assertTrue(all(not entry.item.actionable for entry in result.entries))

    def test_analyze_token_root_and_metadata_are_blocked(self) -> None:
        # Analyze 选中 token 根或 .metadata 时必须只读：清理等于卸载或破坏安装证据。
        # 夹具放在 HOME 内的自定义 prefix，避免系统临时目录的系统路径保护先行命中。
        brew, brew_root = self._home_brew_fixture()
        from openclean.analyzer import analyze_path
        root_entry = next(entry.item for entry in analyze_path(brew_root, workers=1).entries
                          if entry.item.path == brew_root / "fixture")
        self.assertFalse(root_entry.actionable)
        self.assertIn("token 根", root_entry.action_block_reason)
        metadata_entry = next(entry.item for entry in analyze_path(brew_root / "fixture", workers=1).entries
                              if entry.item.path == brew_root / "fixture/.metadata")
        self.assertFalse(metadata_entry.actionable)
        self.assertIn("安装证据", metadata_entry.action_block_reason)
        self.live_processes.return_value = IDLE[0]
        report = execute_cleanup([root_entry, metadata_entry], IgnoreRules(), home=self.home)
        self.assertTrue(all(outcome.status == "blocked" for outcome in report.outcomes))
        self.assertTrue((brew_root / "fixture").exists())
        self.assertTrue((brew_root / "fixture/.metadata").exists())

    def test_analyze_caskroom_root_and_ancestor_cli_preserve_installation(self) -> None:
        brew, brew_root = self._home_brew_fixture()
        os.environ["HOMEBREW_PREFIX"] = str(brew)
        for target in (brew_root, brew):
            with self.subTest(target=target.name):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    status = main(["analyze", str(target.parent), "--select", str(target),
                                   "--yes", "--json"])
                payload = json.loads(output.getvalue())
                self.assertNotEqual(status, 0, payload)
                self.assertTrue((brew_root / "fixture/2.0/bin/tool").exists())
                self.assertTrue((brew / "bin/fixture").exists())
                self.assertEqual(list(self.trash.iterdir()), [])

    def test_caskroom_scope_matrix_preserves_unrelated_siblings(self) -> None:
        brew, root = self._home_brew_fixture()
        token = root / "fixture"
        sibling = brew / "Caskroom-backup"
        sibling.mkdir()
        guards = CleanupGuardContext(IgnoreRules(), lambda: IDLE[0], home=self.home)
        cases = ((brew, True), (root, True), (token, True),
                 (token / ".metadata", True),
                 (token / ".metadata/2.0" / _TIMESTAMP / "Casks", True),
                 (token / "2.0", True), (token / "2.0/bin", True),
                 (token / "1.0", False), (token / "1.0/bin", False), (sibling, False))
        for target, blocked in cases:
            with self.subTest(path=target.relative_to(self.home)):
                self.assertEqual(bool(guards.assess(target).block_reason), blocked)
        alias = root.with_name("CASKROOM")
        if alias.exists() and alias.stat().st_ino == root.stat().st_ino:
            self.assertTrue(guards.assess(alias).block_reason)

    def test_absent_caskroom_does_not_protect_prefix(self) -> None:
        prefix = self.home / "empty-brew"
        prefix.mkdir()
        self.guard_roots.return_value = (prefix / "Caskroom",)
        guards = CleanupGuardContext(IgnoreRules(), lambda: IDLE[0], home=self.home)
        self.assertFalse(guards.assess(prefix).block_reason)

    def test_historical_manpage_blocks_scan_and_execution(self) -> None:
        token = self.make_cask()
        item = self.old(self.scan())
        source = token / "1.0/share/man/man1/fixture-legacy.1"
        source.parent.mkdir(parents=True)
        source.write_text("old manual\n")
        link = self.base / "share/man/man1/fixture-legacy.1"
        link.parent.mkdir(parents=True)
        link.symlink_to(source)
        self.assert_blocked(text="仍指向旧版本")
        report = execute_cleanup([item], IgnoreRules(), home=self.home)
        self.assertEqual(report.outcomes[0].status, "blocked")
        self.assertTrue(link.exists())

    def test_historical_custom_binary_targets_are_checked(self) -> None:
        token = self.make_cask()
        definition = token / ".metadata/1.0/20250101000000.000/Casks/fixture.json"
        definition.parent.mkdir(parents=True)
        for target in ("nested/fixture-legacy", str(self.home / "custom/fixture-legacy")):
            with self.subTest(target=target):
                link = Path(target) if Path(target).is_absolute() else self.base / "bin" / target
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(token / "1.0/bin/tool")
                _write_json(definition, {"version": "1.0", "artifacts": [
                    {"binary": ["bin/tool", {"target": target}]},
                ]})
                self.assert_blocked(text="仍指向旧版本")
                link.unlink()
                # 旧入口已被卸载时仍允许清理；不存在的入口不是错误。
                self.assertTrue(self.old(self.scan()).actionable)

    def test_prefix_residual_link_case_alias_is_checked(self) -> None:
        token = self.make_cask()
        alias = token.with_name("FIXTURE")
        if not alias.exists() or alias.stat().st_ino != token.stat().st_ino:
            self.skipTest("真实大小写别名需不敏感文件系统")
        (self.base / "bin/legacy").symlink_to(alias / "1.0/bin/tool")
        self.assert_blocked(text="仍指向旧版本")

    def test_historical_metadata_is_protected_and_unknown_ruby_is_not_executed(self) -> None:
        token = self.make_cask()
        definition = token / ".metadata/1.0/20250101000000.000/Casks/fixture.json"
        definition.parent.mkdir(parents=True)
        _write_json(definition, {"version": "1.0", "artifacts": []})
        self.assertTrue(self.old(self.scan()).actionable)
        self.assert_blocked(IgnoreRules((str(definition),)), text="忽略")
        definition.unlink()
        ruby = definition.with_suffix(".rb")
        ruby.write_text("raise 'must never execute historical Ruby'\n")
        with mock.patch("subprocess.run", side_effect=AssertionError("must not run Ruby")):
            self.assert_blocked(text="历史 Cask Ruby")

    def test_prefix_tree_protection_cancel_and_budget(self) -> None:
        token = self.make_cask()
        directory = self.base / "share/man/man1"
        directory.mkdir(parents=True)
        (directory / "old.1").symlink_to(token / "1.0/bin/tool")
        self.assert_blocked(IgnoreRules((str(directory),)), text="忽略")
        with mock.patch("openclean.caskroom._PREFIX_LINK_BUDGET", 0), \
             mock.patch("openclean.caskroom._MAX_METADATA_ENTRIES", 200):
            # 大目录消耗真实计数预算，超限不能跳过剩余链接后仍放行。
            for index in range(240):
                (directory / f"page{index}.1").write_text("manual\n")
            self.assert_blocked(text="预算")
        original = caskroom._CaskInspection.probe
        cancelled = False

        def probe(inspection, path):
            nonlocal cancelled
            if path == directory:
                cancelled = True
            return original(inspection, path)

        def checkpoint():
            if cancelled:
                raise Cancelled()

        with mock.patch.object(caskroom._CaskInspection, "probe", probe):
            self.assertTrue(self.scan(checkpoint=checkpoint).cancelled)

    def test_current_entry_outside_prefix_accepts_relative_link(self) -> None:
        token = self.make_cask()
        link = self.home / "tools/fixture"
        link.parent.mkdir()
        link.symlink_to(os.path.relpath(token / "2.0/bin/tool", link.parent))
        _write_json(token / ".metadata/INSTALL_RECEIPT.json", {
            "source": {"version": "2.0"},
            "uninstall_artifacts": [{"binary": ["bin/tool", {"target": str(link)}]}],
        })
        self.assertTrue(self.old(self.scan()).actionable)

    def test_historical_generated_completion_requires_known_output_layout(self) -> None:
        token = self.make_cask()
        definition = token / ".metadata/1.0/20250101000000.000/Casks/fixture.json"
        definition.parent.mkdir(parents=True)
        _write_json(definition, {"version": "1.0", "artifacts": [
            {"generate_completions_from_executable": ["bin/tool", {"shells": ["unknown"]}]},
        ]})
        self.assert_blocked(text="shell 范围不明确")

    def test_unreadable_prefix_inventory_is_not_retried_per_token(self) -> None:
        for index in range(6):
            _make_cask(self.root, f"tool{index}", "2.0", "1.0", "2.0")
        with mock.patch("openclean.caskroom._iter_prefix_links", side_effect=PermissionError("unreadable")) as inventory:
            result = self.scan()
        self.assertEqual(inventory.call_count, 1)
        self.assertTrue(result.items)
        self.assertTrue(all(not item.actionable for item in result.items))

    def test_installation_conflict_keeps_real_entry_intact(self) -> None:
        token = self.make_cask()
        (token / ".metadata/2.0").rename(token / ".metadata/1.0")
        link = self.base / "bin/fixture"
        link.unlink()
        link.symlink_to(token / "1.0/bin/tool")
        self.assert_blocked(text="版本不一致")
        self.assertTrue(link.exists())


class CaskroomExecutionTests(CaskroomFixture):
    def test_execute_moves_only_exact_old_version(self) -> None:
        item = self.item()
        report = execute_cleanup([item], IgnoreRules(), home=self.home)
        self.assertTrue(report.complete)
        self.assertEqual(report.outcomes[0].item.path, self.root / "fixture/1.0")
        self.assertEqual(report.outcomes[0].destination, self.trash / "1.0")
        self.assertTrue((self.trash / "1.0/bin/tool").exists())
        self.assertFalse(item.path.exists())
        self.assertTrue((self.root / "fixture/2.0").exists())
        self.assertTrue((self.root / "fixture/.metadata").exists())
        self.assertTrue((self.base / "bin/fixture").exists())
        self.assertGreaterEqual(self.live_handles.call_count, 2)

    def assert_execution_blocked(self, item, protection=None, text=""):
        report = execute_cleanup([item], protection or IgnoreRules(), home=self.home)
        self.assertFalse(report.complete)
        self.assertIn(text, report.outcomes[0].message)
        self.assertTrue(item.path.exists())
        self.assertEqual(list(self.trash.iterdir()), [])
        return report

    def test_cli_execution_moves_only_selected_old_version(self) -> None:
        token = self.make_cask()
        other = _make_cask(self.root, "second", "2.0", "1.0", "2.0")
        point = next(p for p in DEVELOPER_JUNK if p.scanner == "caskroom-versions")
        output = io.StringIO()
        with mock.patch.dict("openclean.engine.DOMAINS", {"developer": [point]}), contextlib.redirect_stdout(output):
            status = main(["clean", "dev", "--json", "--include-critical", "--select", str(token / "1.0"), "--yes"])
        self.assertEqual(status, 0)
        json.loads(output.getvalue())
        self.assertFalse((token / "1.0").exists())
        self.assertTrue((self.trash / "1.0/bin/tool").exists())
        self.assertTrue((other / "1.0/bin/tool").exists())
        self.assertTrue((token / "2.0/bin/tool").exists())
        self.assertTrue((token / ".metadata/INSTALL_RECEIPT.json").exists())

    def test_analyze_candidate_with_open_handles_blocks(self) -> None:
        # Analyze 生成的同路径候选没有 homebrew-caskroom 范围，
        # 句柄复核必须由共享 guards 补上，不能只在专用范围内检查。
        # 夹具放在 HOME 内的自定义 prefix，模拟真实自定义安装位置。
        brew, brew_root = self._home_brew_fixture()
        token_root = brew_root / "fixture"
        analysis = analyze_path(token_root, workers=1)
        item = next(entry.item for entry in analysis.entries
                    if entry.item.path == token_root / "1.0")
        self.assertTrue(item.actionable, item.action_block_reason)
        self.live_handles.return_value = OpenFileSnapshot((str(item.path / "bin/tool"),))
        self.guard_handles.return_value = self.live_handles.return_value
        report = execute_cleanup([item], IgnoreRules(), home=self.home)
        self.assertEqual(report.outcomes[0].status, "blocked")
        self.assertIn("打开句柄", report.outcomes[0].message)
        self.assertTrue(item.path.exists())

    def test_analyze_file_case_alias_with_open_handle_is_preserved(self) -> None:
        _, root = self._home_brew_fixture()
        actual = root / "fixture/1.0/bin/tool"
        alias_parent = root / "fixture/1.0/BIN"
        if not alias_parent.exists() or alias_parent.stat().st_ino != actual.parent.stat().st_ino:
            self.skipTest("真实大小写别名需不敏感文件系统")
        alias = alias_parent / "tool"
        item = next(entry.item for entry in analyze_path(alias_parent, workers=1).entries
                    if entry.item.path == alias)
        self.assertTrue(item.actionable, item.action_block_reason)
        with actual.open("rb"):
            self.guard_handles.return_value = OpenFileSnapshot((str(actual),))
            report = execute_cleanup([item], IgnoreRules(), home=self.home)
        self.assertEqual(report.outcomes[0].status, "blocked")
        self.assertIn("打开句柄", report.outcomes[0].message)
        self.assertTrue(actual.exists())
        self.assertEqual(list(self.trash.iterdir()), [])

    def test_analyze_current_version_child_blocks_as_current_install(self) -> None:
        # Analyze 选中当前版本内的子目录同样按 token/版本 槽位重判：
        # 当前安装版本不可清理，brew 运行时整体也不可执行。
        brew, brew_root = self._home_brew_fixture()
        current = brew_root / "fixture/2.0"
        analysis = analyze_path(current, workers=1)
        item = next(entry.item for entry in analysis.entries
                    if entry.item.path == current / "bin")
        self.assertFalse(item.actionable)
        self.assertIn("当前安装版本", item.action_block_reason)
        self.live_processes.return_value = ProcessSnapshot(("brew upgrade fixture",))
        report = execute_cleanup([item], IgnoreRules(), home=self.home)
        self.assertEqual(report.outcomes[0].status, "blocked")
        self.assertTrue(item.path.exists())
        self.assertTrue((brew / "bin/fixture").exists())

    def test_analyze_parent_directory_blocks_during_brew(self) -> None:
        self.make_cask()
        analysis = analyze_path(self.root, workers=1)
        item = next(entry.item for entry in analysis.entries
                    if entry.item.path == self.root / "fixture")
        self.live_processes.return_value = ProcessSnapshot(("brew upgrade fixture",))
        report = execute_cleanup([item], IgnoreRules(), home=self.home)
        self.assertFalse(report.complete)
        self.assertTrue(item.path.exists())

    def test_opened_after_scan_blocks(self) -> None:
        item = self.item()
        with (item.path / "bin/tool").open("rb"):
            self.live_handles.return_value = OpenFileSnapshot((str(item.path / "bin/tool"),))
            self.assert_execution_blocked(item, text="打开句柄")

    def test_open_handle_detection_failure_blocks(self) -> None:
        item = self.item()
        self.live_handles.side_effect = OpenFileDetectionError("unavailable")
        self.assert_execution_blocked(item, text="打开句柄")

    def test_handle_opened_after_preflight_blocks_final_move(self) -> None:
        item = self.item()
        def prepare(_):
            self.live_handles.return_value = OpenFileSnapshot((str(item.path / "bin/tool"),))
            return self.trash
        report = execute_cleanup([item], IgnoreRules(), home=self.home, trash_resolver=prepare)
        self.assertFalse(report.complete)
        self.assertIn("打开句柄", report.outcomes[0].message)
        self.assertTrue(item.path.exists())
        self.assertEqual(list(self.trash.iterdir()), [])

    def test_receipt_change_blocks(self) -> None:
        item = self.item()
        receipt = self.root / "fixture/.metadata/INSTALL_RECEIPT.json"
        data = json.loads(receipt.read_text())
        data["source"]["version"] = "1.5"
        _write_json(receipt, data)
        self.assert_execution_blocked(item, text="版本不一致")

    def test_consistent_upgrade_still_invalidates_old_plan(self) -> None:
        item = self.item()
        token = item.path.parent
        (token / "2.0").rename(token / "3.0")
        (token / ".metadata/2.0").rename(token / ".metadata/3.0")
        receipt = token / ".metadata/INSTALL_RECEIPT.json"
        data = json.loads(receipt.read_text())
        data["source"]["version"] = "3.0"
        _write_json(receipt, data)
        link = self.base / "bin/fixture"
        link.unlink()
        link.symlink_to(token / "3.0/bin/tool")
        self.assert_execution_blocked(item, text="版本状态已变化")

    def test_metadata_symlink_added_after_scan_blocks(self) -> None:
        item = self.item()
        metadata = item.path.parent / ".metadata"
        external = self.base / "metadata"
        metadata.rename(external)
        metadata.symlink_to(external, target_is_directory=True)
        self.assert_execution_blocked(item, text="符号链接")

    def test_metadata_ignored_after_scan_blocks(self) -> None:
        item = self.item()
        protection = IgnoreRules((str(item.path.parent / ".metadata"),))
        self.assert_execution_blocked(item, protection, text="忽略")

    def test_installation_changed_after_preflight_blocks_final_move(self) -> None:
        item = self.item()
        link = self.base / "bin/fixture"
        def prepare(_):
            link.unlink()
            link.symlink_to(item.path / "bin/tool")
            return self.trash
        report = execute_cleanup([item], IgnoreRules(), home=self.home, trash_resolver=prepare)
        self.assertFalse(report.complete)
        self.assertIn("仍指向旧版本", report.outcomes[0].message)
        self.assertTrue(item.path.exists())
        self.assertTrue(link.exists())
        self.assertEqual(list(self.trash.iterdir()), [])

    def test_held_candidate_prevents_entire_batch(self) -> None:
        first = self.item()
        other = _make_cask(self.root, "second", "2.0", "1.0", "2.0")
        result = self.scan()
        second = next(item for item in result.items if item.path == other / "1.0")
        self.live_handles.return_value = OpenFileSnapshot((str(second.path / "bin/tool"),))
        report = execute_cleanup([first, second], IgnoreRules(), home=self.home)
        self.assertEqual([outcome.status for outcome in report.outcomes], ["not_run", "blocked"])
        self.assertTrue(first.path.exists())
        self.assertTrue(second.path.exists())
        self.assertEqual(list(self.trash.iterdir()), [])

    def test_guard_rechecks_metadata_without_stored_process_markers(self) -> None:
        item = self.item()
        link = self.base / "bin/fixture"
        link.unlink()
        link.symlink_to(item.path / "bin/tool")
        guards = CleanupGuardContext(IgnoreRules(), lambda: IDLE[0], home=self.home)
        state = guards.assess(item.path)
        self.assertEqual(state.updater.status, "version_unknown")
        self.assertIn("仍指向旧版本", state.block_reason)
        self.assertIn("brew.sh", state.process_markers)

    def test_link_retargeted_after_scan_blocks(self) -> None:
        item = self.item()
        link = self.base / "bin/fixture"
        link.unlink()
        link.symlink_to(item.path / "bin/tool")
        self.assert_execution_blocked(item, text="仍指向旧版本")
        self.assertTrue(link.exists())

    def test_root_changed_after_scan_blocks(self) -> None:
        item = self.item()
        token = item.path.parent
        token.rename(self.base / "old-token")
        _make_cask(self.root, "replacement", "2.0", "1.0", "2.0").rename(token)
        self.assert_execution_blocked(item, text="inode")

    def test_brew_started_after_scan_blocks_without_stored_markers(self) -> None:
        item = replace(self.item(), running_process_markers=())
        self.live_processes.return_value = ProcessSnapshot(("brew upgrade fixture",))
        self.assert_execution_blocked(item, text="正在运行")

    def test_unknown_process_state_after_scan_blocks(self) -> None:
        item = self.item()
        self.live_processes.side_effect = ProcessDetectionError("unavailable")
        self.assert_execution_blocked(item, text="进程检测失败")


class CaskroomRootsTests(unittest.TestCase):
    def test_default_and_absolute_environment_roots(self) -> None:
        with TemporaryDirectory() as temporary:
            prefix = Path(temporary).resolve()
            with mock.patch.dict(os.environ, {"HOMEBREW_PREFIX": str(prefix)}):
                roots = caskroom_roots()
            self.assertIn(Path("/opt/homebrew/Caskroom"), roots)
            self.assertIn(prefix / "Caskroom", roots)

    def test_relative_environment_prefix_ignored(self) -> None:
        with mock.patch.dict(os.environ, {"HOMEBREW_PREFIX": "relative"}):
            roots = caskroom_roots()
        self.assertNotIn(Path.cwd() / "relative/Caskroom", roots)
        self.assertEqual(len(roots), 2)

    def test_environment_default_root_is_deduplicated(self) -> None:
        with mock.patch.dict(os.environ, {"HOMEBREW_PREFIX": "/opt/homebrew"}):
            self.assertEqual(caskroom_roots().count(Path("/opt/homebrew/Caskroom")), 1)


class OpenFileSnapshotAliasTests(unittest.TestCase):
    def test_same_length_file_and_directory_aliases_match(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "Caskroom"
            root.mkdir()
            (root / "tool").write_bytes(b"x")
            alias = root.with_name("CASKROOM")
            if not alias.exists() or alias.stat().st_ino != root.stat().st_ino:
                self.skipTest("真实大小写别名需不敏感文件系统")
            for actual, alternate in ((root, alias), (root / "tool", alias / "TOOL")):
                with self.subTest(name=actual.name):
                    self.assertEqual(OpenFileSnapshot((str(actual),)).count_under(alternate), 1)
                    self.assertEqual(OpenFileSnapshot((str(alternate),)).count_under(actual), 1)

    def test_case_alias_handles_match_by_identity(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "Caskroom"
            root.mkdir()
            alias = root.parent / "CASKROOM"
            if not alias.exists() or alias.stat().st_ino != root.stat().st_ino:
                self.skipTest("真实大小写别名需不敏感文件系统")
            over_alias = OpenFileSnapshot((str(alias / "tool"),))
            over_exact = OpenFileSnapshot((str(root / "tool"),))
            self.assertEqual(over_alias.count_under(root), 1)
            self.assertEqual(over_exact.count_under(alias), 1)
            unrelated = Path(temporary).resolve() / "other"
            unrelated.mkdir()
            self.assertEqual(OpenFileSnapshot((str(unrelated / "tool"),)).count_under(root), 0)


if __name__ == "__main__":
    unittest.main()
