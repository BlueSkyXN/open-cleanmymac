from __future__ import annotations

import os
import plistlib
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from openclean import storage_diagnostics
from openclean.cli import _item_payload
from openclean.engine import IgnoreRules, finalize_overlapping_result
from openclean.knowledge_base import KnowledgeBase
from openclean.macos import DarwinUserCacheDiscovery, DarwinUserTempDiscovery
from openclean.models import FileIdentity, Item, ScanResult
from openclean.processes import (
    DeletedOpenFile,
    DeletedOpenFileSnapshot,
    OpenFileSnapshot,
    ProcessSnapshot,
)
from openclean.storage_diagnostics import (
    RETENTION_RULES,
    BrowserStorageRoot,
    RetentionRule,
    SQLiteRule,
    discover_browser_storage_retention_rules,
    discover_codex_log_partition_rules,
    discover_darwin_transient_retention_rules,
    scan_codex_git_skeletons,
    scan_codex_marketplace_staging,
    scan_crashpad_orphan_sidecars,
    scan_darwin_temp_updater_diagnostics,
    scan_open_unlinked_snapshot,
    scan_retention_diagnostics,
    scan_retention_rules,
    scan_sqlite_rules,
)


class OpenUnlinkedDiagnosticTests(unittest.TestCase):
    def test_reports_unique_files_per_volume_without_becoming_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            device = root.stat().st_dev
            snapshot = DeletedOpenFileSnapshot(
                (
                    DeletedOpenFile(
                        device,
                        10,
                        4096,
                        2,
                        ("Finder", "WorkBuddy"),
                    ),
                    DeletedOpenFile(device, 11, 8192, 1, ("Finder",)),
                )
            )

            result = scan_open_unlinked_snapshot(
                snapshot,
                volume_roots=(root,),
            )

            self.assertTrue(result.complete)
            self.assertEqual(len(result.items), 1)
            item = result.items[0]
            self.assertEqual(item.size, 0)
            self.assertEqual(item.logical_size, 12288)
            self.assertIsNone(item.allocated_size)
            self.assertEqual(item.resource_kind, "filesystem_subset")
            self.assertEqual(item.total_count, 2)
            self.assertIsNone(item.active_count)
            self.assertEqual(item.related_process_count, 2)
            self.assertEqual(item.open_handle_count, 3)
            self.assertEqual(item.diagnostic_kind, "open_unlinked")
            self.assertFalse(item.actionable)
            self.assertFalse(item.preselected)
            self.assertIn("逻辑大小上限", item.note)
            payload = _item_payload(item)
            self.assertEqual(payload["potential_bytes"], 0)
            self.assertEqual(payload["reclaimable_bytes"], 0)
            self.assertEqual(payload["logical_bytes"], 12288)
            self.assertEqual(payload["related_process_count"], 2)
            self.assertEqual(result.total, 0)
            self.assertEqual(result.unsupported_total, 0)

    def test_unknown_device_is_a_nonblocking_mapping_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = scan_open_unlinked_snapshot(
                DeletedOpenFileSnapshot(
                    (DeletedOpenFile(999999, 1, 4096, 1, ("Finder",)),)
                ),
                volume_roots=(Path(tmp),),
            )

            self.assertEqual(result.items, [])
            self.assertEqual(result.issues[0].code, "volume_mapping_failed")
            self.assertFalse(result.issues[0].blocking)

    def test_open_unlinked_volume_item_is_not_absorbed_as_parent_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            identity = FileIdentity.from_stat(root.stat())
            diagnostic = Item(
                root,
                0,
                "open-unlinked",
                "critical",
                logical_size=4096,
                actionable=False,
                action_block_reason="diagnostic",
                identity=identity,
                preselected=False,
                resource_kind="filesystem_subset",
                diagnostic_kind="open_unlinked",
                domain="system",
            )
            child = root / "cache"
            child.mkdir()
            cache = Item(
                child,
                1024,
                "cache",
                identity=FileIdentity.from_stat(child.stat()),
                domain="system",
            )

            finalized = finalize_overlapping_result(
                ScanResult(items=[diagnostic, cache])
            )

            self.assertEqual(set(finalized.items), {diagnostic, cache})

    def test_same_path_readonly_diagnostic_wins_generic_same_domain_item(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            identity = FileIdentity.from_stat(root.stat())
            generic = Item(
                root,
                4096,
                "generic",
                identity=identity,
                domain="system",
            )
            diagnostic = Item(
                root,
                1024,
                "diagnostic",
                "critical",
                actionable=False,
                action_block_reason="diagnostic",
                identity=identity,
                preselected=False,
                resource_kind="filesystem_subset",
                diagnostic_kind="codex_transient",
                domain="system",
            )

            for candidates in ([generic, diagnostic], [diagnostic, generic]):
                with self.subTest(
                    order=[item.category for item in candidates]
                ):
                    finalized = finalize_overlapping_result(
                        ScanResult(items=list(candidates))
                    )

                    self.assertEqual(finalized.items, [diagnostic])


class BrowserStorageDiagnosticTests(unittest.TestCase):
    def test_discovers_only_default_and_numbered_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            browser_root = home / "Browser"
            expected = []
            for profile in ("Default", "Profile 2"):
                cache = browser_root / profile / "Service Worker/CacheStorage"
                cache.mkdir(parents=True)
                file = cache / "entry.bin"
                file.write_bytes(b"cache" * 1024)
                expected.append(cache)
            ignored = (
                browser_root
                / "System Profile"
                / "Service Worker/CacheStorage"
            )
            ignored.mkdir(parents=True)
            (ignored / "entry.bin").write_bytes(b"ignored")

            rules, issues = discover_browser_storage_retention_rules(
                home=home,
                roots=(
                    BrowserStorageRoot(
                        "Browser CacheStorage 保留期",
                        "Browser",
                        ("Browser.app",),
                    ),
                ),
            )

            self.assertEqual(issues, ())
            self.assertEqual({Path(rule.path) for rule in rules}, set(expected))
            result = scan_retention_rules(
                rules,
                IgnoreRules(),
                process_snapshot=ProcessSnapshot(("/Applications/Browser.app",)),
                open_files=OpenFileSnapshot((str(expected[0] / "entry.bin"),)),
            )
            self.assertEqual(len(result.items), 2)
            self.assertTrue(all(not item.actionable for item in result.items))
            self.assertTrue(all(not item.preselected for item in result.items))
            self.assertTrue(all("正在运行" in item.note for item in result.items))

    def test_browser_profile_symlink_is_not_followed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            browser_root = home / "Browser"
            external = home / "external"
            (external / "Service Worker/CacheStorage").mkdir(parents=True)
            browser_root.mkdir()
            (browser_root / "Profile 1").symlink_to(external)

            rules, issues = discover_browser_storage_retention_rules(
                home=home,
                roots=(
                    BrowserStorageRoot(
                        "Browser CacheStorage 保留期",
                        "Browser",
                        ("Browser.app",),
                    ),
                ),
            )

            self.assertEqual(rules, ())
            self.assertEqual(issues, ())

    def test_browser_root_and_cache_chain_symlinks_are_not_followed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            external_root = home / "external-root"
            (
                external_root
                / "Default/Service Worker/CacheStorage"
            ).mkdir(parents=True)
            (home / "root-link").symlink_to(
                external_root,
                target_is_directory=True,
            )

            service_worker_root = home / "service-worker-link" / "Default"
            service_worker_root.mkdir(parents=True)
            external_service_worker = home / "external-service-worker"
            (external_service_worker / "CacheStorage").mkdir(parents=True)
            (service_worker_root / "Service Worker").symlink_to(
                external_service_worker,
                target_is_directory=True,
            )

            cache_root = home / "cache-link" / "Default/Service Worker"
            cache_root.mkdir(parents=True)
            external_cache = home / "external-cache"
            external_cache.mkdir()
            (cache_root / "CacheStorage").symlink_to(
                external_cache,
                target_is_directory=True,
            )

            rules, issues = discover_browser_storage_retention_rules(
                home=home,
                roots=tuple(
                    BrowserStorageRoot(
                        f"{relative} CacheStorage 保留期",
                        relative,
                        ("Browser.app",),
                    )
                    for relative in (
                        "root-link",
                        "service-worker-link",
                        "cache-link",
                    )
                ),
            )

            self.assertEqual(rules, ())
            self.assertEqual(issues, ())


class CodexStructuredStorageDiagnosticTests(unittest.TestCase):
    def test_codex_diagnostic_rejects_symlinked_home_component(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / "home"
            external = base / "external"
            root = home / ".codex/.tmp"
            home.mkdir()
            external.mkdir()
            (home / ".codex").symlink_to(external, target_is_directory=True)
            (external / ".tmp").mkdir()

            result = scan_codex_git_skeletons(
                root,
                IgnoreRules(),
                process_snapshot=ProcessSnapshot(()),
                open_files=OpenFileSnapshot(()),
                anchor=home,
            )

            self.assertEqual(result.items, [])
            self.assertEqual(result.issues[0].code, "diagnostic_root_invalid")

    def test_subset_aggregates_do_not_subtract_descendant_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging = root / "marketplaces/.staging"
            staging.mkdir(parents=True)
            git_item = Item(
                root,
                100,
                "Codex Git 临时空壳",
                "critical",
                actionable=False,
                action_block_reason="diagnostic",
                identity=FileIdentity.from_stat(root.stat()),
                resource_kind="filesystem_subset",
                diagnostic_kind="codex_transient",
                domain="ai",
            )
            staging_item = Item(
                staging,
                1000,
                "Codex marketplace 升级 staging",
                "critical",
                actionable=False,
                action_block_reason="diagnostic",
                identity=FileIdentity.from_stat(staging.stat()),
                resource_kind="filesystem_subset",
                diagnostic_kind="codex_transient",
                domain="ai",
            )

            finalized = finalize_overlapping_result(
                ScanResult(items=[git_item, staging_item])
            )

            self.assertEqual(set(finalized.items), {git_item, staging_item})

    def test_discovers_only_valid_codex_date_partitions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            logs = home / "Library/Logs/com.openai.codex"
            valid = logs / "2026/08/29"
            invalid = logs / "2026/02/31"
            unrelated = logs / "current"
            valid.mkdir(parents=True)
            invalid.mkdir(parents=True)
            unrelated.mkdir()
            (valid / "codex.log").write_bytes(b"log")

            rules, issues = discover_codex_log_partition_rules(home=home)

            self.assertEqual(issues, ())
            self.assertEqual(len(rules), 1)
            self.assertEqual(Path(rules[0].path), valid)
            self.assertIn("2026-08-29", rules[0].category)

    def test_codex_date_partitions_preserve_parent_residual_without_duplication(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            partition = root / "2026/08/29"
            partition.mkdir(parents=True)
            partition_log = partition / "codex.log"
            partition_log.write_bytes(b"partition")
            flat_log = root / "current.log"
            flat_log.write_bytes(b"flat")
            parent_rule = RetentionRule(
                "Codex macOS logs 保留期",
                str(root),
                ("codex",),
            )
            partition_rule = RetentionRule(
                "Codex macOS logs 2026-08-29",
                str(partition),
                ("codex",),
            )

            with mock.patch(
                "openclean.storage_diagnostics.RETENTION_RULES",
                (parent_rule,),
            ), mock.patch(
                "openclean.storage_diagnostics.discover_codex_log_partition_rules",
                return_value=((partition_rule,), ()),
            ), mock.patch(
                "openclean.storage_diagnostics.discover_browser_storage_retention_rules",
                return_value=((), ()),
            ), mock.patch(
                "openclean.storage_diagnostics.discover_darwin_transient_retention_rules",
                return_value=((), ()),
            ), mock.patch(
                "openclean.storage_diagnostics.capture_process_snapshot",
                return_value=ProcessSnapshot(()),
            ), mock.patch(
                "openclean.storage_diagnostics.capture_open_file_snapshot",
                return_value=OpenFileSnapshot(()),
            ):
                result = scan_retention_diagnostics(IgnoreRules())

            self.assertTrue(result.complete)
            finalized = finalize_overlapping_result(result)
            by_path = {item.path: item for item in finalized.items}
            self.assertEqual(set(by_path), {root, partition})
            self.assertEqual(
                by_path[root].size,
                flat_log.stat().st_blocks * 512,
            )
            self.assertEqual(by_path[root].retention_file_count, 1)
            self.assertIsNone(by_path[root].latest_mtime)
            self.assertEqual(by_path[partition].retention_file_count, 1)
            self.assertEqual(
                finalized.total,
                flat_log.stat().st_blocks * 512
                + partition_log.stat().st_blocks * 512,
            )

    def test_marketplace_staging_reports_only_upgrade_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".staging"
            upgrade = root / "marketplace-upgrade-example"
            installed = root / "installed-marketplace"
            upgrade.mkdir(parents=True)
            installed.mkdir()
            pack = upgrade / "objects.pack"
            pack.write_bytes(b"pack" * 1024)
            (installed / "keep.bin").write_bytes(b"keep" * 1024)

            result = scan_codex_marketplace_staging(
                root,
                IgnoreRules(),
                process_snapshot=ProcessSnapshot(("codex app-server",)),
                open_files=OpenFileSnapshot((str(pack),)),
            )

            self.assertEqual(len(result.items), 1)
            item = result.items[0]
            self.assertEqual(item.total_count, 1)
            self.assertIsNone(item.active_count)
            self.assertEqual(item.resource_kind, "filesystem_subset")
            self.assertEqual(item.diagnostic_kind, "codex_transient")
            self.assertEqual(item.open_handle_count, 1)
            self.assertFalse(item.actionable)
            self.assertFalse(item.preselected)
            self.assertIn("不包含已安装 marketplace", item.note)
            self.assertEqual(
                item.allocated_size,
                pack.stat().st_blocks * 512,
            )

    def test_codex_structure_rejects_symlink_ancestors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            real = base / "real"
            staging = real / ".staging"
            (staging / "marketplace-upgrade-example").mkdir(parents=True)
            linked = base / "linked"
            linked.symlink_to(real, target_is_directory=True)

            result = scan_codex_marketplace_staging(
                linked / ".staging",
                IgnoreRules(),
                process_snapshot=ProcessSnapshot(()),
                open_files=OpenFileSnapshot(()),
            )

            self.assertEqual(result.items, [])
            self.assertEqual(result.issues[0].code, "unsafe_symlink_ancestor")
            self.assertFalse(result.issues[0].blocking)

    def test_git_skeleton_does_not_match_a_real_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skeleton = root / "git-empty001"
            (skeleton / "objects").mkdir(parents=True)
            (skeleton / "refs").mkdir()
            (skeleton / "HEAD").write_text(
                "ref: refs/heads/main\n",
                encoding="utf-8",
            )
            real = root / "git-real001"
            (real / "objects/ab").mkdir(parents=True)
            (real / "refs").mkdir()
            (real / "HEAD").write_text(
                "ref: refs/heads/main\n",
                encoding="utf-8",
            )
            (real / "config").write_text("[core]\n", encoding="utf-8")
            (real / "objects/ab/object").write_bytes(b"object")
            invalid_metadata = root / "git-invalid-metadata"
            (invalid_metadata / "objects").mkdir(parents=True)
            (invalid_metadata / "refs").mkdir()
            (invalid_metadata / "HEAD").write_text(
                "ref: refs/heads/main\n",
                encoding="utf-8",
            )
            (invalid_metadata / ".DS_Store").mkdir()

            result = scan_codex_git_skeletons(
                root,
                IgnoreRules(),
                process_snapshot=ProcessSnapshot(()),
                open_files=OpenFileSnapshot(()),
            )

            self.assertEqual(len(result.items), 1)
            item = result.items[0]
            self.assertEqual(item.total_count, 1)
            self.assertIsNone(item.active_count)
            self.assertEqual(item.resource_kind, "filesystem_subset")
            self.assertEqual(item.diagnostic_kind, "codex_transient")
            self.assertFalse(item.actionable)
            self.assertIn("未匹配普通、bare、worktree", item.note)

    def test_ignored_git_candidate_is_not_structurally_inspected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ignored = root / "git-ignored"
            (ignored / "objects").mkdir(parents=True)
            (ignored / "refs").mkdir()
            (ignored / "HEAD").write_text(
                "ref: refs/heads/main\n",
                encoding="utf-8",
            )

            with mock.patch(
                "openclean.storage_diagnostics._is_codex_git_skeleton",
                side_effect=AssertionError("ignored HEAD must not be read"),
            ):
                result = scan_codex_git_skeletons(
                    root,
                    IgnoreRules([str(ignored)]),
                    process_snapshot=ProcessSnapshot(()),
                    open_files=OpenFileSnapshot(()),
                )

            self.assertEqual(result.items, [])
            self.assertEqual(result.issues, [])

    def test_git_skeleton_changed_after_measurement_is_not_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skeleton = root / "git-changing"
            (skeleton / "objects").mkdir(parents=True)
            (skeleton / "refs").mkdir()
            (skeleton / "HEAD").write_text(
                "ref: refs/heads/main\n",
                encoding="utf-8",
            )

            with mock.patch(
                "openclean.storage_diagnostics._is_codex_git_skeleton",
                side_effect=(True, False),
            ):
                result = scan_codex_git_skeletons(
                    root,
                    IgnoreRules(),
                    process_snapshot=ProcessSnapshot(()),
                    open_files=OpenFileSnapshot(()),
                )

            self.assertEqual(result.items, [])

    def test_crashpad_dump_arrival_preserves_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sidecar = root / "arriving_sidecar.json"
            sidecar.write_text("{}", encoding="utf-8")
            original_lstat = storage_diagnostics.lstat_retry
            dump = root / "arriving.dmp"

            def lstat_with_arrival(path: Path):
                if path == dump and not dump.exists():
                    dump.write_bytes(b"dump")
                return original_lstat(path)

            with mock.patch(
                "openclean.storage_diagnostics.lstat_retry",
                side_effect=lstat_with_arrival,
            ):
                result = scan_crashpad_orphan_sidecars(
                    root,
                    IgnoreRules(),
                    process_snapshot=ProcessSnapshot(()),
                    open_files=OpenFileSnapshot(()),
                )

            self.assertEqual(result.items, [])

    def test_crashpad_preserves_pairs_and_reports_only_orphans(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_orphan = root / "old_sidecar.json"
            recent_orphan = root / "recent_sidecar.json"
            paired = root / "paired_sidecar.json"
            dump = root / "paired.dmp"
            old_orphan.write_text("{}", encoding="utf-8")
            recent_orphan.write_text("{}", encoding="utf-8")
            paired.write_text("{}", encoding="utf-8")
            dump.write_bytes(b"dump")
            observed_at = 2_000_000.0
            os.utime(old_orphan, (observed_at - 172800, observed_at - 172800))
            os.utime(recent_orphan, (observed_at - 60, observed_at - 60))

            result = scan_crashpad_orphan_sidecars(
                root,
                IgnoreRules(),
                process_snapshot=ProcessSnapshot(("ChatGPT.app",)),
                open_files=OpenFileSnapshot((str(recent_orphan), str(dump))),
                now=observed_at,
            )

            self.assertEqual(len(result.items), 1)
            item = result.items[0]
            self.assertEqual(item.total_count, 2)
            self.assertIsNone(item.active_count)
            self.assertEqual(item.paired_artifact_count, 1)
            self.assertEqual(item.recent_artifact_count, 1)
            self.assertEqual(item.resource_kind, "filesystem_subset")
            self.assertEqual(item.open_handle_count, 1)
            self.assertEqual(item.diagnostic_kind, "crashpad_pairing")
            self.assertFalse(item.actionable)
            self.assertFalse(item.preselected)
            self.assertIn("保留 1 个与 .dmp 配对", item.note)
            self.assertIn("1 个在 24 小时内更新", item.note)
            self.assertEqual(
                item.allocated_size,
                old_orphan.stat().st_blocks * 512
                + recent_orphan.stat().st_blocks * 512,
            )


class RetentionDiagnosticTests(unittest.TestCase):
    def test_real_world_public_roots_are_diagnostic_only(self) -> None:
        by_path = {rule.path: rule for rule in RETENTION_RULES}

        self.assertIn("~/.cache/codex-runtimes", by_path)
        self.assertIn("~/Library/Logs/WorkBuddy", by_path)
        self.assertIn(
            "~/Library/Application Support/"
            "com.netease.uuremote.updater/download",
            by_path,
        )
        self.assertIn(
            "UURemoteService",
            by_path[
                "~/Library/Application Support/"
                "com.netease.uuremote.updater/download"
            ].process_markers,
        )

    def test_knowledge_base_protection_short_circuits_before_lstat(self) -> None:
        protected = Path("/protected/logs")
        gate = IgnoreRules(
            knowledge_base=KnowledgeBase.from_mapping(
                {
                    "schema_version": 1,
                    "protect": {"paths": [str(protected)]},
                }
            )
        )

        with mock.patch(
            "openclean.storage_diagnostics.lstat_retry",
            side_effect=AssertionError("protected path must not be inspected"),
        ):
            result = scan_retention_rules(
                (RetentionRule("Test logs", str(protected), ("app",)),),
                gate,
                process_snapshot=ProcessSnapshot(()),
                open_files=OpenFileSnapshot(()),
            )

        self.assertEqual(result.items, [])
        self.assertEqual(result.issues, [])

    def test_reports_physical_age_buckets_without_becoming_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            now = time.time()
            files = []
            for index, days in enumerate((8, 15, 31), start=1):
                path = root / f"{index}.log"
                path.write_bytes(bytes([index]) * 8192)
                timestamp = now - days * 86400
                os.utime(path, (timestamp, timestamp))
                files.append(path)

            result = scan_retention_rules(
                (RetentionRule("Test logs", str(root), ("test-app",)),),
                IgnoreRules(),
                process_snapshot=ProcessSnapshot(("/Applications/Test-App",)),
                open_files=OpenFileSnapshot((str(files[0]),)),
                now=now,
            )

            self.assertTrue(result.complete)
            self.assertEqual(len(result.items), 1)
            item = result.items[0]
            allocated = [path.stat().st_blocks * 512 for path in files]
            self.assertEqual(item.size, sum(allocated))
            self.assertEqual(item.retention_file_count, 3)
            self.assertEqual(item.retention_7d_bytes, sum(allocated))
            self.assertEqual(item.retention_14d_bytes, sum(allocated[1:]))
            self.assertEqual(item.retention_30d_bytes, allocated[2])
            self.assertEqual(item.open_handle_count, 1)
            self.assertEqual(item.diagnostic_kind, "retention")
            self.assertFalse(item.actionable)
            self.assertFalse(item.preselected)
            self.assertIn("正在运行", item.note)
            payload = _item_payload(item)
            self.assertEqual(payload["retention_30d_bytes"], allocated[2])
            self.assertEqual(payload["diagnostic_kind"], "retention")

    def test_ignored_root_is_not_inspected_or_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "protected-logs"
            root.mkdir()
            (root / "secret.log").write_text("not read", encoding="utf-8")

            result = scan_retention_rules(
                (RetentionRule("Test logs", str(root), ("app",)),),
                IgnoreRules([str(root)]),
                process_snapshot=ProcessSnapshot(()),
                open_files=OpenFileSnapshot(()),
            )

            self.assertEqual(result.items, [])
            self.assertEqual(result.issues, [])

    def test_retention_root_with_symlink_ancestor_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            real = base / "real"
            logs = real / "logs"
            logs.mkdir(parents=True)
            (logs / "entry.log").write_bytes(b"log")
            linked = base / "linked"
            linked.symlink_to(real, target_is_directory=True)

            result = scan_retention_rules(
                (RetentionRule("linked logs", str(linked / "logs"), ()),),
                IgnoreRules(),
                process_snapshot=ProcessSnapshot(()),
                open_files=OpenFileSnapshot(()),
            )

            self.assertEqual(result.items, [])
            self.assertEqual(result.issues[0].code, "unsafe_symlink_ancestor")
            self.assertFalse(result.issues[0].blocking)


class DarwinTransientDiagnosticTests(unittest.TestCase):
    def test_discovers_only_public_dynamic_name_patterns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            temp_root = root / "T"
            cache_root = root / "C"
            code_sign_root = root / "X"
            for path in (
                temp_root / "go-build123",
                temp_root / "qodercli-natives-v1.2.3-user",
                temp_root / "UURemote",
                temp_root / "private-session-abcdef",
                code_sign_root / "com.google.Chrome.code_sign_clone",
                code_sign_root / "org.example.tool.code_sign_clone",
            ):
                path.mkdir(parents=True)

            with mock.patch(
                "openclean.storage_diagnostics.discover_darwin_user_temp",
                return_value=DarwinUserTempDiscovery(paths=(temp_root,)),
            ), mock.patch(
                "openclean.storage_diagnostics.discover_darwin_user_cache",
                return_value=DarwinUserCacheDiscovery(paths=(cache_root,)),
            ):
                rules, issues = discover_darwin_transient_retention_rules()

            by_name = {Path(rule.path).name: rule for rule in rules}
            self.assertEqual(issues, ())
            self.assertIn("go-build123", by_name)
            self.assertIn("qodercli-natives-v1.2.3-user", by_name)
            self.assertIn("UURemote", by_name)
            self.assertIn("com.google.Chrome.code_sign_clone", by_name)
            self.assertIn("org.example.tool.code_sign_clone", by_name)
            self.assertNotIn("private-session-abcdef", by_name)
            self.assertIn(
                "Google Chrome.app",
                by_name[
                    "com.google.Chrome.code_sign_clone"
                ].process_markers,
            )

    def test_dynamic_roots_remain_non_actionable_retention_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            temp_root = root / "T"
            cache_root = root / "C"
            go_build = temp_root / "go-build123"
            clone = root / "X/com.google.Chrome.code_sign_clone"
            go_build.mkdir(parents=True)
            clone.mkdir(parents=True)
            go_file = go_build / "build.bin"
            clone_file = clone / "clone.bin"
            go_file.write_bytes(b"go" * 4096)
            clone_file.write_bytes(b"clone" * 4096)

            with mock.patch(
                "openclean.storage_diagnostics.RETENTION_RULES",
                (),
            ), mock.patch(
                "openclean.storage_diagnostics.discover_darwin_user_temp",
                return_value=DarwinUserTempDiscovery(paths=(temp_root,)),
            ), mock.patch(
                "openclean.storage_diagnostics.discover_darwin_user_cache",
                return_value=DarwinUserCacheDiscovery(paths=(cache_root,)),
            ), mock.patch(
                "openclean.storage_diagnostics.capture_process_snapshot",
                return_value=ProcessSnapshot(
                    ("/usr/local/go/bin/go build", "/Applications/Google Chrome.app")
                ),
            ), mock.patch(
                "openclean.storage_diagnostics.capture_open_file_snapshot",
                return_value=OpenFileSnapshot((str(go_file), str(clone_file))),
            ), mock.patch(
                "openclean.storage_diagnostics.discover_browser_storage_retention_rules",
                return_value=((), ()),
            ), mock.patch(
                "openclean.storage_diagnostics.discover_codex_log_partition_rules",
                return_value=((), ()),
            ):
                result = scan_retention_diagnostics(IgnoreRules())

            self.assertTrue(result.complete)
            self.assertEqual(len(result.items), 2)
            self.assertTrue(all(not item.actionable for item in result.items))
            self.assertTrue(
                all(item.diagnostic_kind == "retention" for item in result.items)
            )
            self.assertTrue(all(item.open_handle_count == 1 for item in result.items))

    def test_unexpected_cache_root_does_not_guess_the_x_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unexpected = root / "cache"
            unexpected.mkdir()

            with mock.patch(
                "openclean.storage_diagnostics.discover_darwin_user_temp",
                return_value=DarwinUserTempDiscovery(),
            ), mock.patch(
                "openclean.storage_diagnostics.discover_darwin_user_cache",
                return_value=DarwinUserCacheDiscovery(paths=(unexpected,)),
            ):
                rules, issues = discover_darwin_transient_retention_rules()

            self.assertEqual(rules, ())
            self.assertEqual(issues[0].code, "path_discovery_failed")
            self.assertFalse(issues[0].blocking)


class SQLiteDiagnosticTests(unittest.TestCase):
    def _database_with_freelist(self, path: Path) -> None:
        connection = sqlite3.connect(path)
        try:
            connection.execute("CREATE TABLE payloads(value BLOB)")
            connection.executemany(
                "INSERT INTO payloads VALUES (?)",
                ((b"x" * 4096,) for _ in range(512)),
            )
            connection.commit()
            connection.execute("DELETE FROM payloads")
            connection.commit()
        finally:
            connection.close()

    def test_immutable_read_only_scan_reports_freelist_without_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "logs.sqlite"
            self._database_with_freelist(database)
            sidecars = tuple(Path(f"{database}{suffix}") for suffix in ("-wal", "-shm"))
            self.assertFalse(any(path.exists() for path in sidecars))

            result = scan_sqlite_rules(
                (
                    SQLiteRule(
                        "Test SQLite",
                        str(database),
                        ("test-app",),
                        minimum_free_bytes=1,
                    ),
                ),
                IgnoreRules(),
                process_snapshot=ProcessSnapshot(("test-app",)),
                open_files=OpenFileSnapshot((str(database),)),
            )

            self.assertTrue(result.complete)
            self.assertEqual(len(result.items), 1)
            item = result.items[0]
            self.assertEqual(item.diagnostic_kind, "sqlite_freelist")
            self.assertGreater(item.sqlite_freelist_count or 0, 0)
            self.assertGreater(item.sqlite_internal_free_bytes or 0, 0)
            self.assertGreater(item.sqlite_internal_free_ratio or 0, 0)
            self.assertLessEqual(item.size, item.allocated_size or 0)
            self.assertEqual(item.open_handle_count, 1)
            self.assertFalse(item.actionable)
            self.assertFalse(any(path.exists() for path in sidecars))
            payload = _item_payload(item)
            self.assertEqual(payload["diagnostic_kind"], "sqlite_freelist")
            self.assertEqual(
                payload["sqlite_internal_free_bytes"],
                item.sqlite_internal_free_bytes,
            )

    def test_non_sqlite_file_is_a_nonblocking_diagnostic_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "not-a-database.sqlite"
            database.write_bytes(b"not sqlite")

            result = scan_sqlite_rules(
                (
                    SQLiteRule(
                        "Test SQLite",
                        str(database),
                        ("test-app",),
                        minimum_free_bytes=1,
                    ),
                ),
                IgnoreRules(),
                process_snapshot=ProcessSnapshot(()),
                open_files=OpenFileSnapshot(()),
            )

            self.assertTrue(result.complete)
            self.assertEqual(result.items, [])
            self.assertEqual(result.issues[0].code, "sqlite_diagnostic_failed")
            self.assertFalse(result.issues[0].blocking)


class DarwinTempUpdaterDiagnosticTests(unittest.TestCase):
    def test_dynamic_shipit_copy_is_visible_but_never_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp) / "T"
            staging = temp_root / "com.aliyun.lingma.ide.ShipIt.test"
            contents = staging / "Qoder CN IDE.app/Contents"
            contents.mkdir(parents=True)
            (contents / "Info.plist").write_bytes(
                plistlib.dumps(
                    {
                        "CFBundleIdentifier": "com.aliyun.lingma.ide",
                        "CFBundleShortVersionString": "1.27.0",
                    }
                )
            )
            (contents / "payload.bin").write_bytes(b"payload")

            with mock.patch(
                "openclean.storage_diagnostics.discover_darwin_user_temp",
                return_value=DarwinUserTempDiscovery(paths=(temp_root,)),
            ), mock.patch(
                "openclean.storage_diagnostics.capture_process_snapshot",
                return_value=ProcessSnapshot(()),
            ), mock.patch(
                "openclean.storage_diagnostics.capture_open_file_snapshot",
                return_value=OpenFileSnapshot(()),
            ), mock.patch(
                "openclean.updater._application_roots",
                return_value=(Path(tmp) / "Applications",),
            ):
                result = scan_darwin_temp_updater_diagnostics(IgnoreRules())

            self.assertTrue(result.complete)
            self.assertEqual(len(result.items), 1)
            item = result.items[0]
            self.assertEqual(item.diagnostic_kind, "updater_temp")
            self.assertEqual(item.updater_status, "installed_app_missing")
            self.assertEqual(item.staged_version, "1.27.0")
            self.assertFalse(item.actionable)
            self.assertFalse(item.preselected)


if __name__ == "__main__":
    unittest.main()
