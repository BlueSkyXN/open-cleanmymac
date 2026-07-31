from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from openclean import cleanup as cleanup_module
from openclean.cleanup import (
    SelectionError,
    execute_cleanup,
    select_cleanup_items,
    with_cleanup_selection,
)
from openclean.docker import DockerPruneResult
from openclean.engine import IgnoreRules, scan_points
from openclean.knowledge_base import KnowledgeBase
from openclean.models import FileFacts, Item, ScanResult
from openclean.scanpoints import ScanPoint

TEST_SF_DATALESS = 0x40000000


def _with_dataless_flag(stat_result):
    return SimpleNamespace(
        st_mode=stat_result.st_mode,
        st_size=stat_result.st_size,
        st_blocks=stat_result.st_blocks,
        st_mtime=stat_result.st_mtime,
        st_dev=stat_result.st_dev,
        st_ino=stat_result.st_ino,
        st_nlink=stat_result.st_nlink,
        st_uid=stat_result.st_uid,
        st_flags=TEST_SF_DATALESS,
    )


def _with_zero_blocks(stat_result):
    values = vars(_with_dataless_flag(stat_result)).copy()
    values.update(st_blocks=0, st_flags=0)
    return SimpleNamespace(**values)


def _candidate(
    path: Path | None,
    category: str,
    safety: str,
    *,
    preselected: bool,
    actionable: bool = True,
    identifier: str = "",
) -> Item:
    return Item(
        path=path,
        size=100,
        category=category,
        safety=safety,
        preselected=preselected,
        domain="developer",
        resource_kind="filesystem" if path is not None else "docker",
        identifier=identifier,
        actionable=actionable,
        action_block_reason=("执行器不可用" if not actionable else ""),
    )


class CleanupSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.safe = _candidate(
            Path("/Users/example/safe"),
            "safe",
            "safe",
            preselected=True,
        )
        self.recent = _candidate(
            Path("/Users/example/recent"),
            "recent",
            "safe",
            preselected=False,
        )
        self.confirm = _candidate(
            Path("/Users/example/confirm"),
            "confirm",
            "confirm",
            preselected=False,
        )
        self.critical = _candidate(
            Path("/Users/example/critical"),
            "critical",
            "critical",
            preselected=False,
        )
        self.blocked = _candidate(
            None,
            "docker",
            "safe",
            preselected=False,
            actionable=False,
            identifier="docker:build-cache",
        )
        self.items = [
            self.safe,
            self.recent,
            self.confirm,
            self.critical,
            self.blocked,
        ]

    def test_default_and_explicit_safety_tiers(self) -> None:
        self.assertEqual(select_cleanup_items(self.items), [self.safe])
        self.assertEqual(
            select_cleanup_items(self.items, select_all_safe=True),
            [self.safe, self.recent],
        )
        self.assertEqual(
            select_cleanup_items(self.items, include_confirm=True),
            [self.safe, self.confirm],
        )
        self.assertEqual(
            select_cleanup_items(self.items, include_critical=True),
            [self.safe, self.critical],
        )

    def test_explicit_confirm_and_critical_require_matching_gate(self) -> None:
        with self.assertRaisesRegex(SelectionError, "--include-confirm"):
            select_cleanup_items(
                self.items,
                selectors=[str(self.confirm.path)],
            )
        with self.assertRaisesRegex(SelectionError, "--include-critical"):
            select_cleanup_items(
                self.items,
                selectors=[str(self.critical.path)],
            )

        self.assertEqual(
            select_cleanup_items(
                self.items,
                selectors=[str(self.confirm.path)],
                include_confirm=True,
            ),
            [self.confirm],
        )
        self.assertEqual(
            select_cleanup_items(
                self.items,
                selectors=[str(self.critical.path)],
                include_critical=True,
            ),
            [self.critical],
        )

    def test_exact_selection_rejects_batch_all(self) -> None:
        with self.assertRaisesRegex(SelectionError, "不能与 --all"):
            select_cleanup_items(
                self.items,
                selectors=[str(self.recent.path)],
                select_all_safe=True,
            )

    def test_environment_candidate_requires_exact_selection(self) -> None:
        environment = replace(
            self.confirm,
            path_source="environment",
            requires_explicit_selection=True,
        )

        selected_by_tier = select_cleanup_items(
            [environment],
            include_confirm=True,
        )
        selected_exactly = select_cleanup_items(
            [environment],
            selectors=[str(environment.path)],
            include_confirm=True,
        )

        self.assertEqual(selected_by_tier, [])
        self.assertEqual(selected_exactly, [environment])

    def test_unknown_and_non_actionable_selectors_are_rejected(self) -> None:
        with self.assertRaisesRegex(SelectionError, "未找到"):
            select_cleanup_items(self.items, selectors=["missing"])
        with self.assertRaisesRegex(SelectionError, "执行器不可用"):
            select_cleanup_items(
                self.items,
                selectors=["docker:build-cache"],
            )

    def test_selection_snapshot_does_not_mutate_scan_items(self) -> None:
        result = ScanResult(items=list(self.items))
        selected = select_cleanup_items(
            self.items,
            include_confirm=True,
        )

        marked = with_cleanup_selection(result, selected)

        by_category = {item.category: item for item in marked.items}
        self.assertTrue(by_category["safe"].preselected)
        self.assertTrue(by_category["confirm"].preselected)
        self.assertFalse(by_category["critical"].preselected)
        self.assertFalse(self.confirm.preselected)


class CleanupExecutionTests(unittest.TestCase):
    def _scan_directory(self, path: Path, *, domain: str = "developer") -> Item:
        result = scan_points(
            [ScanPoint("测试候选", (str(path),), domain=domain)],
            workers=1,
        )
        self.assertEqual(len(result.items), 1)
        return result.items[0]

    def test_moves_candidate_to_same_volume_trash_without_free_space_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            cache = home / "Library" / "Caches" / "tool"
            cache.mkdir(parents=True)
            (cache / "data.bin").write_bytes(b"cache-data")
            item = self._scan_directory(cache)
            self.assertEqual(item.identity.owner, os.getuid())

            report = execute_cleanup([item], IgnoreRules(), home=home)

            self.assertTrue(report.complete)
            self.assertFalse(cache.exists())
            outcome = report.outcomes[0]
            self.assertEqual(outcome.status, "moved_to_trash")
            self.assertIsNotNone(outcome.destination)
            self.assertTrue(outcome.destination.exists())
            self.assertEqual(outcome.destination.parent, home / ".Trash")
            self.assertEqual(report.moved_bytes, item.size)
            self.assertEqual(report.deleted_bytes, 0)
            self.assertIn("尚未实际释放", outcome.message)

    def test_existing_trash_name_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            source = home / "work" / "cache"
            source.mkdir(parents=True)
            (source / "new.bin").write_bytes(b"new")
            trash = home / ".Trash"
            existing = trash / "cache"
            existing.mkdir(parents=True)
            (existing / "old.bin").write_bytes(b"old")
            item = self._scan_directory(source)

            report = execute_cleanup([item], IgnoreRules(), home=home)

            destination = report.outcomes[0].destination
            self.assertEqual(destination, trash / "cache 2")
            self.assertTrue((existing / "old.bin").exists())
            self.assertTrue((destination / "new.bin").exists())

    def test_trash_name_created_after_check_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            source = home / "payload.bin"
            trash = home / ".Trash"
            home.mkdir()
            trash.mkdir()
            source.write_bytes(b"source")
            item = self._scan_directory(source)
            original_stat = os.stat
            inserted = False

            def racing_stat(path, *args, **kwargs):
                nonlocal inserted
                try:
                    return original_stat(path, *args, **kwargs)
                except FileNotFoundError:
                    directory_fd = kwargs.get("dir_fd")
                    if not inserted and directory_fd is not None:
                        descriptor = os.open(
                            path,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                            0o600,
                            dir_fd=directory_fd,
                        )
                        try:
                            os.write(descriptor, b"existing-trash-data")
                        finally:
                            os.close(descriptor)
                        inserted = True
                    raise

            with mock.patch(
                "openclean.cleanup.os.stat", side_effect=racing_stat
            ):
                report = execute_cleanup(
                    [item],
                    IgnoreRules(),
                    home=home,
                    trash_resolver=lambda _: trash,
                )

            self.assertTrue(inserted)
            self.assertFalse(report.complete)
            self.assertEqual(report.outcomes[0].status, "failed")
            self.assertEqual((trash / source.name).read_bytes(), b"existing-trash-data")
            self.assertEqual(source.read_bytes(), b"source")

    def test_source_recreated_after_rename_does_not_hide_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            source = home / "payload.bin"
            trash = home / ".Trash"
            home.mkdir()
            trash.mkdir()
            source.write_bytes(b"original")
            item = self._scan_directory(source)
            original_stat = os.stat
            source_parent_fd = None
            recreated = False

            def racing_stat(path, *args, **kwargs):
                nonlocal source_parent_fd, recreated
                directory_fd = kwargs.get("dir_fd")
                try:
                    stat_result = original_stat(path, *args, **kwargs)
                except FileNotFoundError:
                    if directory_fd == source_parent_fd and not recreated:
                        descriptor = os.open(
                            path,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                            0o600,
                            dir_fd=directory_fd,
                        )
                        try:
                            os.write(descriptor, b"recreated")
                        finally:
                            os.close(descriptor)
                        recreated = True
                        return original_stat(path, *args, **kwargs)
                    raise
                if (
                    source_parent_fd is None
                    and directory_fd is not None
                    and stat_result.st_ino == item.identity.inode
                ):
                    source_parent_fd = directory_fd
                return stat_result

            with mock.patch(
                "openclean.cleanup.os.stat", side_effect=racing_stat
            ):
                report = execute_cleanup(
                    [item],
                    IgnoreRules(),
                    home=home,
                    trash_resolver=lambda _: trash,
                )

            outcome = report.outcomes[0]
            self.assertTrue(recreated)
            self.assertTrue(report.complete)
            self.assertEqual(outcome.status, "moved_to_trash")
            self.assertEqual(outcome.destination, trash / source.name)
            self.assertEqual(outcome.destination.read_bytes(), b"original")
            self.assertEqual(source.read_bytes(), b"recreated")
            self.assertIn("源路径已出现新对象", outcome.message)

    def test_rule_change_blocks_whole_batch_before_any_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            first = home / "first"
            second = home / "second"
            first.mkdir(parents=True)
            second.mkdir()
            (first / "a.bin").write_bytes(b"a")
            protected = second / "protected.bin"
            protected.write_bytes(b"protected")
            first_item = self._scan_directory(first)
            second_item = self._scan_directory(second)
            protection = IgnoreRules(
                knowledge_base=KnowledgeBase.from_mapping(
                    {
                        "schema_version": 1,
                        "protect": {"paths": [str(protected)]},
                    }
                )
            )

            report = execute_cleanup(
                [first_item, second_item],
                protection,
                home=home,
            )

            self.assertFalse(report.complete)
            self.assertTrue(first.exists())
            self.assertTrue(second.exists())
            self.assertEqual(
                [outcome.status for outcome in report.outcomes],
                ["not_run", "blocked"],
            )
            self.assertFalse((home / ".Trash").exists())

    def test_execution_phase_failure_is_reported_after_prior_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            first = home / "first"
            second = home / "second"
            trash = home / ".Trash"
            first.mkdir(parents=True)
            second.mkdir()
            trash.mkdir()
            (first / "a.bin").write_bytes(b"a")
            (second / "b.bin").write_bytes(b"b")
            first_item = self._scan_directory(first)
            second_item = self._scan_directory(second)
            calls = 0

            def resolver(path: Path) -> Path:
                nonlocal calls
                calls += 1
                if calls == 2:
                    replaced = home / "second-original"
                    path.rename(replaced)
                    path.mkdir()
                return trash

            report = execute_cleanup(
                [first_item, second_item],
                IgnoreRules(),
                home=home,
                trash_resolver=resolver,
            )

            self.assertFalse(report.complete)
            self.assertEqual(
                [outcome.status for outcome in report.outcomes],
                ["moved_to_trash", "failed"],
            )
            self.assertTrue((trash / "first" / "a.bin").exists())
            self.assertTrue((home / "second").exists())
            self.assertTrue((home / "second-original" / "b.bin").exists())

    def test_protected_descendant_added_after_preflight_blocks_move(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            cache = home / "cache"
            trash = home / ".Trash"
            cache.mkdir(parents=True)
            trash.mkdir()
            (cache / "original.bin").write_bytes(b"original")
            item = self._scan_directory(cache)
            protected = cache / "protected.bin"
            protection = IgnoreRules(
                knowledge_base=KnowledgeBase.from_mapping(
                    {
                        "schema_version": 1,
                        "protect": {"paths": [str(protected)]},
                    }
                )
            )

            def resolver(_: Path) -> Path:
                protected.write_bytes(b"protected")
                return trash

            report = execute_cleanup(
                [item],
                protection,
                home=home,
                trash_resolver=resolver,
            )

            self.assertFalse(report.complete)
            self.assertEqual(report.outcomes[0].status, "failed")
            self.assertIn("命中忽略或保护规则", report.outcomes[0].message)
            self.assertTrue(cache.is_dir())
            self.assertTrue(protected.is_file())
            self.assertEqual(list(trash.iterdir()), [])

    def test_refuses_cross_filesystem_trash_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            cache = home / "cache"
            cache.mkdir(parents=True)
            (cache / "data.bin").write_bytes(b"data")
            item = self._scan_directory(cache)

            report = execute_cleanup(
                [item],
                IgnoreRules(),
                home=home,
                trash_resolver=lambda _: Path("/dev"),
            )

            self.assertFalse(report.complete)
            self.assertIn("不在同一文件系统", report.outcomes[0].message)
            self.assertTrue(cache.exists())

    def test_inode_replacement_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            cache = home / "cache"
            cache.mkdir(parents=True)
            (cache / "old.bin").write_bytes(b"old")
            item = self._scan_directory(cache)
            old = home / "old-cache"
            cache.rename(old)
            cache.mkdir()
            (cache / "new.bin").write_bytes(b"new")

            report = execute_cleanup([item], IgnoreRules(), home=home)

            self.assertFalse(report.complete)
            self.assertEqual(report.outcomes[0].status, "blocked")
            self.assertIn("inode 已变化", report.outcomes[0].message)
            self.assertTrue(cache.exists())
            self.assertTrue(old.exists())

    def test_symlink_replacement_is_blocked_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            cache = home / "cache"
            target = home / "important"
            cache.mkdir(parents=True)
            target.mkdir()
            (cache / "old.bin").write_bytes(b"old")
            important = target / "keep.bin"
            important.write_bytes(b"keep")
            item = self._scan_directory(cache)
            old = home / "old-cache"
            cache.rename(old)
            cache.symlink_to(target, target_is_directory=True)

            report = execute_cleanup([item], IgnoreRules(), home=home)

            self.assertFalse(report.complete)
            self.assertTrue(cache.is_symlink())
            self.assertTrue(important.exists())

    def test_symlink_ancestor_is_rejected_by_scan_and_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            target = home / "Documents" / "important"
            target.mkdir(parents=True)
            keep = target / "keep.bin"
            keep.write_bytes(b"keep")
            alias_root = home / "cache-link"
            alias_root.symlink_to(home / "Documents", target_is_directory=True)
            alias = alias_root / "important"

            with mock.patch.dict("os.environ", {"HOME": str(home)}, clear=False):
                scan_result = scan_points(
                    [ScanPoint("测试候选", (str(alias),))],
                    workers=1,
                )

            facts = FileFacts.from_path(alias)
            item = Item(
                path=alias,
                size=keep.stat().st_blocks * 512,
                category="测试候选",
                identity=facts.identity,
                domain="developer",
            )
            report = execute_cleanup([item], IgnoreRules(), home=home)

            self.assertEqual(scan_result.items, [])
            self.assertEqual(
                scan_result.issues[0].code,
                "unsafe_symlink_ancestor",
            )
            self.assertFalse(report.complete)
            self.assertIn("符号链接组件", report.outcomes[0].message)
            self.assertTrue(keep.is_file())
            self.assertFalse((home / ".Trash").exists())

    def test_new_cloud_placeholder_blocks_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            cache = home / "cache"
            cache.mkdir(parents=True)
            local = cache / "local.bin"
            local.write_bytes(b"local")
            item = self._scan_directory(cache)
            cloud = cache / "cloud.bin"
            cloud.write_bytes(b"remote")
            cloud_stat = cloud.lstat()
            original_lstat = Path.lstat

            def fake_lstat(path: Path):
                stat_result = original_lstat(path)
                if path == cloud:
                    return SimpleNamespace(
                        st_mode=cloud_stat.st_mode,
                        st_size=cloud_stat.st_size,
                        st_blocks=0,
                        st_mtime=cloud_stat.st_mtime,
                        st_dev=cloud_stat.st_dev,
                        st_ino=cloud_stat.st_ino,
                        st_nlink=cloud_stat.st_nlink,
                        st_uid=cloud_stat.st_uid,
                    )
                return stat_result

            with mock.patch.object(Path, "lstat", new=fake_lstat):
                report = execute_cleanup([item], IgnoreRules(), home=home)

            self.assertFalse(report.complete)
            self.assertIn("云占位文件", report.outcomes[0].message)
            self.assertTrue(cache.exists())

    def test_candidate_that_becomes_dataless_blocks_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            cache = home / "cache"
            cache.mkdir(parents=True)
            (cache / "local.bin").write_bytes(b"local")
            item = self._scan_directory(cache)
            cache_stat = cache.lstat()
            original_lstat = Path.lstat

            def fake_lstat(path: Path):
                stat_result = original_lstat(path)
                return (
                    _with_dataless_flag(cache_stat)
                    if path == cache
                    else stat_result
                )

            with mock.patch(
                "openclean.models.MACOS_SF_DATALESS", TEST_SF_DATALESS
            ), mock.patch.object(Path, "lstat", new=fake_lstat):
                report = execute_cleanup([item], IgnoreRules(), home=home)

            self.assertFalse(report.complete)
            self.assertIn("dataless", report.outcomes[0].message)
            self.assertTrue(cache.exists())

    def test_dataless_descendant_is_rejected_before_scandir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            cache = home / "cache"
            child = cache / "remote"
            child.mkdir(parents=True)
            (child / "payload.bin").write_bytes(b"payload")
            item = self._scan_directory(cache)
            child_stat = child.lstat()
            original_lstat = Path.lstat
            original_scandir = os.scandir

            def fake_lstat(path: Path):
                stat_result = original_lstat(path)
                return (
                    _with_dataless_flag(child_stat)
                    if path == child
                    else stat_result
                )

            def guarded_scandir(path):
                if not isinstance(path, int) and Path(path) == child:
                    raise AssertionError("dataless descendant must not be enumerated")
                return original_scandir(path)

            with mock.patch(
                "openclean.models.MACOS_SF_DATALESS", TEST_SF_DATALESS
            ), mock.patch.object(Path, "lstat", new=fake_lstat), mock.patch(
                "openclean.cleanup.os.scandir", side_effect=guarded_scandir
            ):
                report = execute_cleanup([item], IgnoreRules(), home=home)

            self.assertFalse(report.complete)
            self.assertIn("dataless", report.outcomes[0].message)
            self.assertTrue(cache.exists())

    def test_descendant_changed_to_symlink_is_not_enumerated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            cache = home / "cache"
            child = cache / "child"
            outside = home / "outside"
            child.mkdir(parents=True)
            outside.mkdir()
            (child / "cache.bin").write_bytes(b"cache")
            protected = outside / "important.bin"
            protected.write_bytes(b"important")
            item = self._scan_directory(cache)
            original_lstat = cleanup_module._lstat
            original_scandir = os.scandir
            outside_stat = outside.lstat()
            saved_child = cache / "saved-child"
            swapped = False
            scanned_outside = False

            def racing_lstat(path: Path):
                nonlocal swapped
                stat_result = original_lstat(path)
                if path == child and not swapped:
                    child.rename(saved_child)
                    child.symlink_to(outside, target_is_directory=True)
                    swapped = True
                return stat_result

            def guarded_scandir(path):
                nonlocal scanned_outside
                if isinstance(path, int):
                    live = os.fstat(path)
                    scanned_outside = scanned_outside or (
                        live.st_dev == outside_stat.st_dev
                        and live.st_ino == outside_stat.st_ino
                    )
                return original_scandir(path)

            with mock.patch(
                "openclean.cleanup._lstat", side_effect=racing_lstat
            ), mock.patch(
                "openclean.cleanup.os.scandir", side_effect=guarded_scandir
            ):
                report = execute_cleanup([item], IgnoreRules(), home=home)

            self.assertFalse(report.complete)
            self.assertIn("普通目录", report.outcomes[0].message)
            self.assertFalse(scanned_outside)
            self.assertEqual(protected.read_bytes(), b"important")

    def test_final_fd_stat_dataless_check_prevents_rename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            cache = home / "cache"
            trash = home / ".Trash"
            cache.mkdir(parents=True)
            trash.mkdir()
            (cache / "local.bin").write_bytes(b"local")
            item = self._scan_directory(cache)
            original_stat_at = cleanup_module._stat_at

            def dataless_source_stat(directory_fd: int, name: str, path: Path):
                stat_result = original_stat_at(directory_fd, name, path)
                if path == cache:
                    return _with_dataless_flag(stat_result)
                return stat_result

            with mock.patch(
                "openclean.models.MACOS_SF_DATALESS", TEST_SF_DATALESS
            ), mock.patch(
                "openclean.cleanup._stat_at", side_effect=dataless_source_stat
            ), mock.patch("openclean.cleanup._rename_no_replace") as rename:
                report = execute_cleanup(
                    [item],
                    IgnoreRules(),
                    home=home,
                    trash_resolver=lambda _: trash,
                )

            rename.assert_not_called()
            self.assertFalse(report.complete)
            self.assertIn("dataless", report.outcomes[0].message)
            self.assertTrue(cache.exists())

    def test_final_fd_stat_zero_block_fallback_prevents_rename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            cache = home / "cache.bin"
            trash = home / ".Trash"
            home.mkdir()
            trash.mkdir()
            cache.write_bytes(b"local")
            item = self._scan_directory(cache)
            original_stat_at = cleanup_module._stat_at

            def zero_block_source_stat(
                directory_fd: int, name: str, path: Path
            ):
                stat_result = original_stat_at(directory_fd, name, path)
                if path == cache:
                    return _with_zero_blocks(stat_result)
                return stat_result

            with mock.patch(
                "openclean.cleanup._stat_at",
                side_effect=zero_block_source_stat,
            ), mock.patch("openclean.cleanup._rename_no_replace") as rename:
                report = execute_cleanup(
                    [item],
                    IgnoreRules(),
                    home=home,
                    trash_resolver=lambda _: trash,
                )

            rename.assert_not_called()
            self.assertFalse(report.complete)
            self.assertIn("疑似云占位", report.outcomes[0].message)
            self.assertTrue(cache.exists())

    def test_empty_trash_preserves_root_and_external_symlink_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            trash = home / ".Trash"
            nested = trash / "folder"
            nested.mkdir(parents=True)
            (trash / "file.bin").write_bytes(b"trash")
            (nested / "nested.bin").write_bytes(b"nested")
            outside = home / "important.bin"
            outside.write_bytes(b"important")
            (trash / "outside-link").symlink_to(outside)
            item = self._scan_directory(trash, domain="trash")

            report = execute_cleanup(
                [item],
                IgnoreRules(),
                home=home,
                uid=os.getuid(),
            )

            self.assertTrue(report.complete)
            self.assertEqual(report.outcomes[0].status, "deleted")
            self.assertTrue(trash.is_dir())
            self.assertEqual(list(trash.iterdir()), [])
            self.assertTrue(outside.exists())
            self.assertEqual(report.deleted_bytes, item.size)

    def test_empty_trash_preserves_entries_added_after_final_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            trash = home / ".Trash"
            trash.mkdir(parents=True)
            old = trash / "old.bin"
            arrived = trash / "arrived-after-audit.bin"
            old.write_bytes(b"old")
            item = self._scan_directory(trash, domain="trash")
            protection = IgnoreRules(
                knowledge_base=KnowledgeBase.from_mapping(
                    {
                        "schema_version": 1,
                        "protect": {"paths": [str(arrived)]},
                    }
                )
            )
            original_audit = cleanup_module._audit_descendants
            audit_calls = 0

            def racing_audit(*args, **kwargs):
                nonlocal audit_calls
                snapshot = original_audit(*args, **kwargs)
                audit_calls += 1
                if audit_calls == 3:
                    arrived.write_bytes(b"keep")
                return snapshot

            with mock.patch(
                "openclean.cleanup._audit_descendants",
                side_effect=racing_audit,
            ):
                report = execute_cleanup(
                    [item],
                    protection,
                    home=home,
                    uid=os.getuid(),
                )

            self.assertEqual(audit_calls, 3)
            self.assertTrue(report.complete)
            self.assertEqual(report.outcomes[0].status, "deleted")
            self.assertFalse(old.exists())
            self.assertEqual(arrived.read_bytes(), b"keep")
            self.assertIn("保留审计后新增", report.outcomes[0].message)

    def test_empty_trash_preserves_nested_entry_added_after_final_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            trash = home / ".Trash"
            folder = trash / "folder"
            folder.mkdir(parents=True)
            old = folder / "old.bin"
            arrived = folder / "arrived-after-audit.bin"
            old.write_bytes(b"old")
            item = self._scan_directory(trash, domain="trash")
            original_audit = cleanup_module._audit_descendants
            audit_calls = 0

            def racing_audit(*args, **kwargs):
                nonlocal audit_calls
                snapshot = original_audit(*args, **kwargs)
                audit_calls += 1
                if audit_calls == 3:
                    arrived.write_bytes(b"keep")
                return snapshot

            with mock.patch(
                "openclean.cleanup._audit_descendants",
                side_effect=racing_audit,
            ):
                report = execute_cleanup(
                    [item],
                    IgnoreRules(),
                    home=home,
                    uid=os.getuid(),
                )

            outcome = report.outcomes[0]
            self.assertEqual(audit_calls, 3)
            self.assertFalse(report.complete)
            self.assertEqual(outcome.status, "partial")
            self.assertFalse(old.exists())
            self.assertEqual(arrived.read_bytes(), b"keep")
            self.assertIn("实际释放容量无法可靠确定", outcome.message)

    def test_missing_atomic_rename_api_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            source = home / "payload.bin"
            trash = home / ".Trash"
            home.mkdir()
            trash.mkdir()
            source.write_bytes(b"source")
            item = self._scan_directory(source)

            with mock.patch("openclean.cleanup._RENAMEATX_NP", None):
                report = execute_cleanup(
                    [item],
                    IgnoreRules(),
                    home=home,
                    trash_resolver=lambda _: trash,
                )

            self.assertFalse(report.complete)
            self.assertEqual(report.outcomes[0].status, "failed")
            self.assertIn("缺少 renameatx_np", report.outcomes[0].message)
            self.assertEqual(source.read_bytes(), b"source")
            self.assertEqual(list(trash.iterdir()), [])

    def test_empty_trash_reports_partial_after_some_permanent_deletes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            trash = home / ".Trash"
            trash.mkdir(parents=True)
            first = trash / "a.bin"
            second = trash / "b.bin"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            item = self._scan_directory(trash, domain="trash")
            original_unlink = os.unlink
            calls = 0

            def partial_unlink(path, *args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise PermissionError("injected failure")
                return original_unlink(path, *args, **kwargs)

            with mock.patch(
                "openclean.cleanup.os.unlink", side_effect=partial_unlink
            ):
                report = execute_cleanup(
                    [item],
                    IgnoreRules(),
                    home=home,
                    uid=os.getuid(),
                )

            outcome = report.outcomes[0]
            self.assertFalse(report.complete)
            self.assertEqual(outcome.status, "partial")
            self.assertEqual(outcome.bytes_affected, 0)
            self.assertFalse(first.exists())
            self.assertTrue(second.exists())
            self.assertIn("已永久删除 1/2", outcome.message)
            self.assertIn("无法可靠确定", outcome.message)

    def test_refuses_non_trash_root_and_privileged_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            arbitrary = home / "ordinary"
            arbitrary.mkdir(parents=True)
            (arbitrary / "data.bin").write_bytes(b"data")
            trash_item = self._scan_directory(arbitrary, domain="trash")
            privileged = replace(
                self._scan_directory(arbitrary),
                requires_privilege=True,
            )

            trash_report = execute_cleanup(
                [trash_item], IgnoreRules(), home=home
            )
            privileged_report = execute_cleanup(
                [privileged], IgnoreRules(), home=home
            )

            self.assertFalse(trash_report.complete)
            self.assertIn("非 Trash 根目录", trash_report.outcomes[0].message)
            self.assertFalse(privileged_report.complete)
            self.assertIn("特权帮助器", privileged_report.outcomes[0].message)
            self.assertTrue(arbitrary.exists())

    def test_empty_selection_is_a_successful_noop(self) -> None:
        report = execute_cleanup([], IgnoreRules())

        self.assertTrue(report.complete)
        self.assertEqual(report.outcomes, [])

    def test_docker_prune_uses_unified_report_without_filesystem_identity(self) -> None:
        item = Item(
            path=None,
            size=2_000_000,
            category="Docker 构建缓存",
            preselected=True,
            domain="developer",
            resource_kind="docker",
            identifier="docker:build-cache",
        )

        with mock.patch(
            "openclean.cleanup.prune_docker_resource",
            return_value=DockerPruneResult(1_500_000, "pruned"),
        ) as prune:
            report = execute_cleanup([item], IgnoreRules())

        self.assertTrue(report.complete)
        self.assertEqual(report.outcomes[0].status, "pruned")
        self.assertEqual(report.deleted_bytes, 1_500_000)
        self.assertEqual(report.moved_bytes, 0)
        prune.assert_called_once_with(
            "docker:build-cache",
            docker_path=None,
            runner=None,
            finder=None,
        )

    def test_docker_local_volumes_remain_hard_blocked(self) -> None:
        item = Item(
            path=None,
            size=2_000_000,
            category="Docker 本地卷",
            safety="critical",
            domain="developer",
            resource_kind="docker",
            identifier="docker:local-volumes",
            actionable=False,
            action_block_reason="Docker 本地卷不支持自动清理",
        )

        with self.assertRaisesRegex(SelectionError, "不支持自动清理"):
            select_cleanup_items(
                [item],
                selectors=["docker:local-volumes"],
                include_critical=True,
            )

    def test_candidate_without_scan_identity_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            path = home / "cache"
            path.mkdir(parents=True)
            (path / "data.bin").write_bytes(b"data")
            item = Item(path=path, size=4, category="手工候选")

            report = execute_cleanup([item], IgnoreRules(), home=home)

            self.assertFalse(report.complete)
            self.assertIn("缺少扫描时 inode", report.outcomes[0].message)
            self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
