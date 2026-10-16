from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from openclean.caskroom import (
    assess_cask_version,
    caskroom_roots,
    scan_caskroom_versions,
)
from openclean.cleanup import execute_cleanup
from openclean.cleanup_guards import CleanupGuardContext
from openclean.engine import IgnoreRules
from openclean.models import Item
from openclean.processes import OpenFileSnapshot, ProcessSnapshot

IDLE = (ProcessSnapshot(()), OpenFileSnapshot(()))


def _make_cask(root: Path, token: str, receipt_version: str, *versions: str) -> Path:
    token_root = root / token
    (token_root / ".metadata").mkdir(parents=True)
    json.dump(
        {"source": {"version": receipt_version}},
        (token_root / ".metadata" / "INSTALL_RECEIPT.json").open("w"),
    )
    for version in versions:
        (token_root / version / "bin").mkdir(parents=True)
        (token_root / version / "bin" / "tool").write_bytes(b"x" * 4096)
    return token_root


class AssessCaskVersionTests(unittest.TestCase):
    def test_older_directory_is_residue(self) -> None:
        state = assess_cask_version("codex", "0.162.1", "0.137.0")
        self.assertEqual(state.status, "older_version_residue")
        self.assertFalse(state.blocks_cleanup)

    def test_same_version_blocks(self) -> None:
        state = assess_cask_version("codex", "0.137.0", "0.137.0")
        self.assertEqual(state.status, "same_version_residue")
        self.assertTrue(state.blocks_cleanup)

    def test_higher_directory_blocks(self) -> None:
        state = assess_cask_version("codex", "0.137.0", "0.162.1")
        self.assertEqual(state.status, "pending_update")
        self.assertTrue(state.blocks_cleanup)

    def test_non_numeric_version_is_unknown(self) -> None:
        # 1.0.98-2 这类带后缀版本按不可比较处理，不猜语义。
        state = assess_cask_version("x", "1.0.98-2", "1.0.98-2")
        self.assertEqual(state.status, "version_unknown")
        self.assertTrue(state.blocks_cleanup)


class ScanCaskroomTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "Caskroom"
        self.root.mkdir()
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(os.environ, {"HOMEBREW_PREFIX": str(self.root.parent)}, clear=False).start()

    def test_old_version_directory_becomes_candidate(self) -> None:
        _make_cask(self.root, "codex", "0.162.1", "0.137.0", "0.162.1")
        result = scan_caskroom_versions(IgnoreRules(), snapshots=IDLE)
        self.assertEqual(len(result.items), 2)
        old = next(i for i in result.items if i.path.name == "0.137.0")
        self.assertEqual(old.updater_status, "older_version_residue")
        self.assertTrue(old.actionable)
        self.assertEqual(old.safety, "critical")
        self.assertFalse(old.preselected)
        self.assertTrue(old.requires_explicit_selection)
        self.assertIsNotNone(old.identity)
        self.assertEqual(old.domain, "developer")
        self.assertGreater(old.size, 0)
        current = next(i for i in result.items if i.path.name == "0.162.1")
        self.assertFalse(current.actionable)
        self.assertIn("当前安装版本", current.action_block_reason)

    def test_single_version_token_not_reported(self) -> None:
        _make_cask(self.root, "iterm2", "3.7.4", "3.7.4")
        result = scan_caskroom_versions(IgnoreRules(), snapshots=IDLE)
        self.assertEqual(result.items, [])

    def test_missing_receipt_blocks(self) -> None:
        token_root = self.root / "codex"
        (token_root / "0.137.0" / "bin").mkdir(parents=True)
        (token_root / "0.162.1" / "bin").mkdir(parents=True)
        (token_root / "0.162.1" / "bin" / "tool").write_bytes(b"x" * 100)
        result = scan_caskroom_versions(IgnoreRules(), snapshots=IDLE)
        self.assertEqual(len(result.items), 2)
        self.assertTrue(all(not i.actionable for i in result.items))
        self.assertTrue(all(i.updater_status == "version_unknown" for i in result.items))

    def test_brew_running_blocks(self) -> None:
        _make_cask(self.root, "codex", "0.162.1", "0.137.0", "0.162.1")
        snapshot = ProcessSnapshot(
            ("/bin/bash -p /opt/homebrew/Library/Homebrew/brew.sh update",),
        )
        result = scan_caskroom_versions(IgnoreRules(), snapshots=(snapshot, IDLE[1]))
        old = next(i for i in result.items if i.path.name == "0.137.0")
        self.assertFalse(old.actionable)
        self.assertIn("Homebrew 正在运行", old.action_block_reason)

    def test_unknown_process_state_blocks(self) -> None:
        _make_cask(self.root, "codex", "0.162.1", "0.137.0", "0.162.1")
        result = scan_caskroom_versions(IgnoreRules(), snapshots=(None, None))
        self.assertTrue(result.items)
        self.assertTrue(all(not i.actionable for i in result.items))
        self.assertTrue(all("进程状态未知" in i.action_block_reason for i in result.items))

    def test_open_handles_block(self) -> None:
        token = _make_cask(self.root, "codex", "0.162.1", "0.137.0", "0.162.1")
        old_dir = token / "0.137.0"
        snapshot = OpenFileSnapshot((str(old_dir / "bin" / "tool"),))
        result = scan_caskroom_versions(IgnoreRules(), snapshots=(IDLE[0], snapshot))
        old = next(i for i in result.items if i.path.name == "0.137.0")
        self.assertFalse(old.actionable)
        self.assertIn("存在打开句柄", old.action_block_reason)

    def test_symlink_version_directory_rejected(self) -> None:
        _make_cask(self.root, "codex", "0.162.1", "0.162.1")
        outside = self.root.parent / "outside"
        outside.mkdir()
        (outside / "payload").write_bytes(b"x" * 100)
        os.symlink(outside, self.root / "codex" / "0.137.0")
        result = scan_caskroom_versions(IgnoreRules(), snapshots=IDLE)
        olds = [i for i in result.items if i.path.name == "0.137.0"]
        self.assertEqual(olds, [])

    def test_ignored_path_excluded(self) -> None:
        token = _make_cask(self.root, "codex", "0.162.1", "0.137.0", "0.162.1")
        result = scan_caskroom_versions(
            IgnoreRules((str(token / "0.137.0"),)), snapshots=IDLE,
        )
        olds = [i for i in result.items if i.path.name == "0.137.0"]
        self.assertEqual(olds, [])

    def test_metadata_directory_not_a_version(self) -> None:
        _make_cask(self.root, "codex", "0.162.1", "0.162.1")
        result = scan_caskroom_versions(IgnoreRules(), snapshots=IDLE)
        self.assertEqual(result.items, [])

    def test_receipt_change_reassessed_by_guards(self) -> None:
        token = _make_cask(self.root, "codex", "0.162.1", "0.137.0", "0.162.1")
        ctx = CleanupGuardContext(IgnoreRules(), lambda: None, home=Path.home())
        old = token / "0.137.0"
        assessment = ctx.assess(old)
        assert assessment.updater is not None
        self.assertEqual(assessment.updater.status, "older_version_residue")
        self.assertEqual(assessment.block_reason, "")
        # brew upgrade 后 receipt 指向新版本，0.162.1 变成旧版本
        json.dump(
            {"source": {"version": "0.170.0"}},
            (token / ".metadata" / "INSTALL_RECEIPT.json").open("w"),
        )
        assessment = ctx.assess(token / "0.162.1")
        assert assessment.updater is not None
        self.assertEqual(assessment.updater.status, "older_version_residue")
        # 原候选的执行复核四元组不再匹配
        assessment = ctx.assess(old)
        assert assessment.updater is not None
        self.assertEqual(assessment.updater.installed_version, "0.170.0")


class CaskroomExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name).resolve()
        self.home = base / "home"
        self.trash = self.home / ".Trash"
        self.trash.mkdir(parents=True)
        self.trash.chmod(0o700)
        self.root = base / "Caskroom"
        self.root.mkdir()
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(os.environ, {"HOME": str(self.home),
                                     "HOMEBREW_PREFIX": str(base)}, clear=False).start()

    def _item(self) -> Item:
        _make_cask(self.root, "codex", "0.162.1", "0.137.0", "0.162.1")
        result = scan_caskroom_versions(IgnoreRules(), snapshots=IDLE)
        item = next(i for i in result.items if i.path.name == "0.137.0")
        self.assertTrue(item.actionable)
        return item

    def test_execute_moves_old_version_to_trash(self) -> None:
        item = self._item()
        with mock.patch("openclean.cleanup.capture_process_snapshot",
                        return_value=IDLE[0]):
            outcome = execute_cleanup([item], IgnoreRules(), home=self.home)
        self.assertTrue(outcome.complete)
        self.assertFalse(item.path.exists())
        self.assertTrue((self.root / "codex" / "0.162.1").exists())
        self.assertTrue((self.root / "codex" / ".metadata").exists())

    def test_execute_rejected_when_receipt_changed(self) -> None:
        item = self._item()
        json.dump(
            {"source": {"version": "0.150.0"}},
            (self.root / "codex" / ".metadata" / "INSTALL_RECEIPT.json").open("w"),
        )
        with mock.patch("openclean.cleanup.capture_process_snapshot",
                        return_value=IDLE[0]):
            outcome = execute_cleanup([item], IgnoreRules(), home=self.home)
        self.assertFalse(outcome.complete)
        self.assertTrue(item.path.exists())


class CaskroomRootsTests(unittest.TestCase):
    def test_default_and_env_roots(self) -> None:
        with mock.patch.dict(os.environ, {"HOMEBREW_PREFIX": "/custom/prefix"}, clear=False):
            roots = caskroom_roots()
        self.assertIn(Path("/opt/homebrew/Caskroom"), roots)
        self.assertIn(Path("/custom/prefix/Caskroom"), roots)

    def test_relative_env_prefix_ignored(self) -> None:
        with mock.patch.dict(os.environ, {"HOMEBREW_PREFIX": "relative"}, clear=False):
            roots = caskroom_roots()
        self.assertNotIn(Path("relative/Caskroom"), roots)


if __name__ == "__main__":
    unittest.main()
