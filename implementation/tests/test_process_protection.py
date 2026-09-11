from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from openclean.application_ownership import process_markers_for_path
from openclean.cleanup import SelectionError, execute_cleanup, select_cleanup_items
from openclean.cli import main
from openclean.engine import IgnoreRules, finalize_overlapping_result, scan_points
from openclean import engine
from openclean.models import Item, ScanResult
from openclean.macos import DarwinUserCacheDiscovery
from openclean.processes import (
    DeletedOpenFileSnapshot,
    OpenFileDetectionError,
    OpenFileSnapshot,
    ProcessDetectionError,
    ProcessSnapshot,
    capture_deleted_open_file_snapshot,
    capture_open_file_snapshot,
    capture_process_snapshot,
    parse_deleted_open_files,
)
from openclean.scanpoints import (
    AI_TOOL_JUNK,
    DEVELOPER_JUNK,
    DOMAINS,
    SYSTEM_JUNK,
    ScanPoint,
)


class ProcessSnapshotTests(unittest.TestCase):
    def test_capture_and_case_insensitive_marker_matching(self) -> None:
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(
                command,
                0,
                "/Applications/Xcode.app/Contents/MacOS/Xcode\n"
                "/usr/bin/python worker.py\n",
                "",
            )

        snapshot = capture_process_snapshot(runner=runner)

        self.assertEqual(calls[0][0], ["/bin/ps", "-axo", "command="])
        self.assertTrue(snapshot.any_running(("xcode.app",)))
        self.assertFalse(snapshot.any_running(("codex",)))
        self.assertEqual(len(snapshot.commands), 2)

    def test_capture_failure_and_timeout_are_explicit(self) -> None:
        with self.assertRaisesRegex(ProcessDetectionError, "ps failed"):
            capture_process_snapshot(
                runner=lambda command, **_: subprocess.CompletedProcess(
                    command, 1, "", "ps failed"
                )
            )

        def timeout(command, **_):
            raise subprocess.TimeoutExpired(command, 0.01)

        with self.assertRaisesRegex(ProcessDetectionError, "0.01 秒"):
            capture_process_snapshot(runner=timeout, timeout=0.01)

    def test_open_file_snapshot_parses_paths_and_counts_scopes(self) -> None:
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(
                command,
                0,
                "p10\nfcwd\nn/Users/example\n"
                "f5\nn/Users/example/logs/a.log\n"
                "f6\nn/Users/example/state.sqlite-wal\n",
                "",
            )

        snapshot = capture_open_file_snapshot(runner=runner)

        self.assertEqual(calls[0][0], ["/usr/sbin/lsof", "-nP", "-Fn"])
        self.assertEqual(snapshot.count_under("/Users/example/logs"), 1)
        self.assertEqual(
            snapshot.count_sqlite_family("/Users/example/state.sqlite"),
            1,
        )

    def test_open_file_snapshot_failure_is_explicit(self) -> None:
        with self.assertRaisesRegex(OpenFileDetectionError, "denied"):
            capture_open_file_snapshot(
                runner=lambda command, **_: subprocess.CompletedProcess(
                    command, 1, "", "denied"
                )
            )
        snapshot = OpenFileSnapshot(("/tmp/root/file",))
        self.assertEqual(snapshot.count_under("/tmp/root"), 1)

    def test_deleted_open_parser_deduplicates_device_inode_and_handles(self) -> None:
        snapshot = parse_deleted_open_files(
            "p10\ncWorkBuddy Helper\nf1\nD0x100000d\ni42\ns100\n"
            "f2\nD0x100000d\ni42\ns100\n"
            "p11\ncSpotlight\nf3\nD0x100000d\ni42\ns100\n"
            "f4\nD0x100001b\ni42\ns50\n"
        )

        self.assertIsInstance(snapshot, DeletedOpenFileSnapshot)
        self.assertEqual(len(snapshot.files), 2)
        system = snapshot.files[0]
        self.assertEqual(system.device, 0x100000D)
        self.assertEqual(system.inode, 42)
        self.assertEqual(system.logical_size, 100)
        self.assertEqual(system.handle_count, 3)
        self.assertEqual(system.commands, ("Spotlight", "WorkBuddy Helper"))

    def test_deleted_open_capture_uses_path_only_for_protection_filtering(self) -> None:
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(
                command,
                0,
                (
                    "p10\ncFinder\nf7\nD0x100000d\ni99\ns4096\n"
                    "n/Users/alice/SecretProject/cache.bin (deleted)\n"
                ),
                "",
            )

        snapshot = capture_deleted_open_file_snapshot(runner=runner)

        self.assertEqual(
            calls[0][0],
            ["/usr/sbin/lsof", "-nP", "-w", "+L1", "-FpcfDisn"],
        )
        self.assertEqual(snapshot.files[0].logical_size, 4096)
        self.assertEqual(snapshot.files[0].commands, ("Finder",))
        self.assertEqual(
            snapshot.files[0].paths,
            ("/Users/alice/SecretProject/cache.bin",),
        )
        self.assertNotIn("SecretProject", repr(snapshot))

    def test_deleted_open_capture_never_exposes_raw_lsof_stderr(self) -> None:
        secret = "/Users/alice/SecretProject"

        with self.assertRaises(OpenFileDetectionError) as raised:
            capture_deleted_open_file_snapshot(
                runner=lambda command, **_: subprocess.CompletedProcess(
                    command,
                    1,
                    "",
                    f"lsof: WARNING: can't stat() {secret}\nsecond line",
                )
            )

        message = str(raised.exception)
        self.assertEqual(message, "lsof +L1 检测失败，退出码 1")
        self.assertNotIn(secret, message)
        self.assertNotIn("second line", message)

    def test_deleted_open_capture_accepts_empty_lsof_result(self) -> None:
        snapshot = capture_deleted_open_file_snapshot(
            runner=lambda command, **_: subprocess.CompletedProcess(
                command, 1, "", ""
            )
        )
        self.assertEqual(snapshot.files, ())

    def test_deleted_open_capture_rejects_unparseable_output(self) -> None:
        with self.assertRaisesRegex(OpenFileDetectionError, "无法解析"):
            capture_deleted_open_file_snapshot(
                runner=lambda command, **_: subprocess.CompletedProcess(
                    command, 0, "p10\ncFinder\nf7\nDbad\niunknown\nsbad\n", ""
                )
            )

    def test_deleted_open_parser_rejects_partially_malformed_output(self) -> None:
        with self.assertRaisesRegex(OpenFileDetectionError, "device 字段无效"):
            parse_deleted_open_files(
                "p10\ncFinder\nf7\nD0x100000d\ni99\ns4096\n"
                "f8\nDbad\ni100\ns8192\n"
            )


class ProcessProtectedScanTests(unittest.TestCase):
    def test_clean_dev_reuses_other_domains_process_protection(self) -> None:
        cases = (
            (".cache/opencode", "opencode serve"),
            (".cache/chrome-devtools-mcp/chrome-profile/Default/Cache", "chrome-devtools-mcp"),
        )
        for relative, command in cases:
            for failed in (False, True):
                with self.subTest(relative=relative, failed=failed), tempfile.TemporaryDirectory() as tmp:
                    home = Path(tmp).resolve()
                    cache = home / relative
                    cache.mkdir(parents=True)
                    (cache / "live.bin").write_bytes(b"cache")
                    (home / ".Trash").mkdir(mode=0o700)
                    rules = home / "rules.json"
                    rules.write_text('{"schema_version": 1}', encoding="utf-8")
                    uv = next(p for p in DEVELOPER_JUNK if p.category == "uv 缓存")
                    stdout = io.StringIO()
                    with mock.patch.dict(os.environ, {"HOME": str(home), "UV_CACHE_DIR": str(cache)}), mock.patch.dict(
                        DOMAINS, {"developer": [uv]},
                    ), mock.patch(
                        "openclean.engine.capture_process_snapshot",
                        return_value=ProcessSnapshot((command,)),
                        side_effect=ProcessDetectionError("unavailable") if failed else None,
                    ), contextlib.redirect_stdout(stdout):
                        status = main(["clean", "dev", "--select", str(cache), "--include-confirm",
                                       "--yes", "--json", "--rules", str(rules)])
                    self.assertEqual(status, 2)
                    self.assertEqual(json.loads(stdout.getvalue())["error"]["code"], "selection_error")
                    self.assertTrue((cache / "live.bin").exists())
                    self.assertEqual(list((home / ".Trash").iterdir()), [])

    def test_registered_owner_matches_descendants_and_present_children_not_siblings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            owner = home / ".cache/opencode"
            child = owner / "nested"
            sibling = home / ".cache/opencode-backup"
            for directory in (child, sibling):
                directory.mkdir(parents=True)
                (directory / "entry").write_bytes(b"cache")
            uv = next(p for p in DEVELOPER_JUNK if p.category == "uv 缓存")
            for target, blocked in ((child, True), (owner.parent, True), (sibling, False)):
                with self.subTest(target=target.name), mock.patch.dict(
                    os.environ, {"HOME": str(home), "UV_CACHE_DIR": str(target)},
                ), mock.patch("openclean.engine.capture_process_snapshot", return_value=ProcessSnapshot(("opencode",))):
                    item = next(i for i in scan_points([uv], workers=1).items if i.path == target)
                    self.assertEqual(item.actionable, not blocked)

    def test_registered_owner_is_rechecked_at_execution(self) -> None:
        for running in (False, True):
            with self.subTest(running=running), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp).resolve()
                cache = home / ".cache/opencode"
                cache.mkdir(parents=True)
                (cache / "entry").write_bytes(b"cache")
                trash = home / ".Trash"
                trash.mkdir(mode=0o700)
                uv = next(p for p in DEVELOPER_JUNK if p.category == "uv 缓存")
                with mock.patch.dict(os.environ, {"HOME": str(home), "UV_CACHE_DIR": str(cache)}), mock.patch(
                    "openclean.engine.capture_process_snapshot", return_value=ProcessSnapshot(()),
                ):
                    result = scan_points([uv], workers=1)
                    selected = select_cleanup_items(result.items, selectors=[str(cache)], include_confirm=True)
                    report = execute_cleanup(
                        selected, IgnoreRules(), home=home, trash_resolver=lambda _: trash,
                        process_runner=lambda command, **_: subprocess.CompletedProcess(
                            command, 0, "opencode\n" if running else "", "",
                        ),
                    )
                self.assertEqual(report.complete, not running)
                self.assertEqual(cache.exists(), running)

    def test_registered_parent_does_not_probe_below_dataless(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            cloud = home / ".cache"
            (cloud / "opencode").mkdir(parents=True)
            point = ScanPoint("container", (str(cloud),), process_owner_protection=True)
            with mock.patch.dict(os.environ, {"HOME": str(home)}), mock.patch(
                "openclean.models.FileFacts.is_dataless", new=property(lambda facts: facts.path == cloud),
            ), mock.patch("openclean.engine._inspect_path", wraps=engine._inspect_path) as inspect, mock.patch(
                "openclean.engine.capture_process_snapshot", return_value=ProcessSnapshot(()),
            ):
                result = scan_points([point], workers=1)
            self.assertTrue(result.complete)
            self.assertEqual(len(result.items), 1)
            self.assertFalse(result.items[0].actionable)
            self.assertFalse(any(Path(call.args[0]).is_relative_to(cloud) and Path(call.args[0]) != cloud
                                 for call in inspect.call_args_list))

    def test_environment_cache_cannot_bypass_running_owner_or_failed_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            cache = home / "Library/Caches/com.openai.codex"
            cache.mkdir(parents=True)
            (cache / "entry.bin").write_bytes(b"cache")
            uv = replace(next(p for p in DEVELOPER_JUNK if p.category == "uv 缓存"),
                         domain="developer")
            generic = replace(next(p for p in SYSTEM_JUNK if p.category == "用户缓存"),
                              domain="system")
            for points in ([uv], [generic, uv]):
                for failure in (False, True):
                    with self.subTest(generic=len(points) == 2, failure=failure):
                        with mock.patch.dict(os.environ, {"HOME": str(home), "UV_CACHE_DIR": str(cache)}), mock.patch(
                            "openclean.engine.capture_process_snapshot",
                            return_value=ProcessSnapshot(("/Applications/Codex.app/Contents/MacOS/Codex",)),
                            side_effect=ProcessDetectionError("unavailable") if failure else None,
                        ):
                            result = finalize_overlapping_result(scan_points(points, workers=1))
                        self.assertEqual(len(result.items), 1)
                        self.assertFalse(result.items[0].actionable)
                        with self.assertRaises(SelectionError):
                            select_cleanup_items(result.items, selectors=[str(cache)], include_confirm=True)
            self.assertTrue((cache / "entry.bin").exists())

    def test_same_path_merge_preserves_block_and_late_start_protection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve() / "cache"
            generic = Item(path, 4096, "generic", domain="system",
                           running_process_markers=("Example.app",))
            specific = Item(path, 4096, "specific", domain="developer", preselected=True)
            for blocked in (False, True):
                guarded = replace(generic, actionable=not blocked,
                                  action_block_reason="正在运行" if blocked else "")
                for items in ([guarded, specific], [specific, guarded]):
                    with self.subTest(blocked=blocked, order=items[0].category):
                        merged = finalize_overlapping_result(ScanResult(items=items)).items[0]
                        self.assertEqual(merged.category, "specific")
                        self.assertEqual(merged.running_process_markers, ("Example.app",))
                        self.assertEqual(merged.actionable, not blocked)
                        if blocked:
                            self.assertFalse(merged.preselected)
                            self.assertIn("正在运行", merged.action_block_reason)

    def test_environment_owner_is_rechecked_before_execution(self) -> None:
        for running in (False, True):
            with self.subTest(running=running), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp).resolve()
                cache = home / "Library/Caches/com.openai.codex"
                cache.mkdir(parents=True)
                (cache / "entry.bin").write_bytes(b"cache")
                trash = home / ".Trash"
                trash.mkdir(mode=0o700)
                uv = next(p for p in DEVELOPER_JUNK if p.category == "uv 缓存")
                with mock.patch.dict(os.environ, {"HOME": str(home), "UV_CACHE_DIR": str(cache)}), mock.patch(
                    "openclean.engine.capture_process_snapshot", return_value=ProcessSnapshot(()),
                ):
                    result = scan_points([uv], workers=1)
                    selected = select_cleanup_items(result.items, selectors=[str(cache)], include_confirm=True)
                    report = execute_cleanup(
                        selected, IgnoreRules(), home=home, trash_resolver=lambda _: trash,
                        process_runner=lambda command, **_: subprocess.CompletedProcess(
                            command, 0, "Codex\n" if running else "", "",
                        ),
                    )
                self.assertEqual(report.complete, not running)
                self.assertEqual(cache.exists(), running)
                self.assertEqual((trash / "com.openai.codex/entry.bin").exists(), not running)

    def test_running_tool_is_visible_but_non_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache"
            cache.mkdir()
            (cache / "data.bin").write_bytes(b"data")
            point = ScanPoint(
                "Codex 缓存",
                (str(cache),),
                running_process_markers=("codex",),
            )

            with mock.patch(
                "openclean.engine.capture_process_snapshot",
                return_value=ProcessSnapshot(("/Applications/Codex.app/Codex",)),
            ):
                result = scan_points([point], workers=1)

            self.assertTrue(result.complete)
            self.assertEqual(len(result.items), 1)
            self.assertFalse(result.items[0].actionable)
            self.assertFalse(result.items[0].preselected)
            self.assertIn("正在运行", result.items[0].action_block_reason)
            self.assertEqual(result.issues[0].code, "resource_in_use")
            self.assertFalse(result.issues[0].blocking)

    def test_process_detection_failure_blocks_only_protected_scan_points(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            protected = root / "protected"
            ordinary = root / "ordinary"
            protected.mkdir()
            ordinary.mkdir()
            (protected / "a.bin").write_bytes(b"a")
            (ordinary / "b.bin").write_bytes(b"b")
            points = [
                ScanPoint(
                    "Protected",
                    (str(protected),),
                    running_process_markers=("tool",),
                ),
                ScanPoint("Ordinary", (str(ordinary),)),
            ]

            with mock.patch(
                "openclean.engine.capture_process_snapshot",
                side_effect=ProcessDetectionError("ps unavailable"),
            ):
                result = scan_points(points, workers=1)

            self.assertFalse(result.complete)
            by_category = {item.category: item for item in result.items}
            self.assertEqual(set(by_category), {"Protected", "Ordinary"})
            self.assertFalse(by_category["Protected"].actionable)
            self.assertIn(
                "无法确认",
                by_category["Protected"].action_block_reason,
            )
            self.assertTrue(by_category["Ordinary"].actionable)
            self.assertEqual(result.issues[0].code, "process_detection_failed")
            self.assertTrue(result.issues[0].blocking)

    def test_generic_user_cache_applies_application_owner_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            caches = home / "Library" / "Caches"
            codex_cache = caches / "com.openai.codex"
            codex_cache.mkdir(parents=True)
            (codex_cache / "data.bin").write_bytes(b"data")
            point = ScanPoint(
                "用户缓存",
                (str(caches),),
                "confirm",
                expand_children=True,
                process_owner_protection=True,
            )

            with mock.patch.dict("os.environ", {"HOME": str(home)}), mock.patch(
                "openclean.engine.capture_process_snapshot",
                return_value=ProcessSnapshot(
                    ("/Applications/ChatGPT.app/Contents/MacOS/ChatGPT",)
                ),
            ):
                result = scan_points([point], workers=1)

            self.assertEqual(len(result.items), 1)
            item = result.items[0]
            self.assertEqual(item.path, codex_cache)
            self.assertFalse(item.actionable)
            self.assertIn("ChatGPT.app", item.running_process_markers)
            self.assertIn("正在运行", item.action_block_reason)

    def test_application_owner_rules_do_not_match_similar_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            owned = home / "Library" / "Caches" / "com.openai.codex"
            sibling = home / "Library" / "Caches" / "com.openai.codex-backup"
            uuremote = home / "Library" / "Caches" / "com.netease.uuremote"

            self.assertIn(
                "ChatGPT.app",
                process_markers_for_path(owned, home=home),
            )
            self.assertEqual(
                process_markers_for_path(sibling, home=home),
                (),
            )
            self.assertIn(
                "UURemote.app",
                process_markers_for_path(uuremote, home=home),
            )

    def test_darwin_cache_owner_rules_require_the_discovered_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_root = root / "C"
            owned = cache_root / "com.netease.uuremote"

            self.assertIn(
                "UURemote.app",
                process_markers_for_path(
                    owned,
                    home=root / "home",
                    darwin_cache_root=cache_root,
                ),
            )
            self.assertIn(
                "Cursor.app",
                process_markers_for_path(
                    cache_root / "com.todesktop.230313mzl4w4u92.helper",
                    home=root / "home",
                    darwin_cache_root=cache_root,
                ),
            )
            self.assertEqual(
                process_markers_for_path(
                    root / "other/com.netease.uuremote",
                    home=root / "home",
                    darwin_cache_root=cache_root,
                ),
                (),
            )

    def test_generic_darwin_cache_applies_application_owner_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            cache_root = root / "C"
            owned = cache_root / "com.netease.uuremote"
            owned.mkdir(parents=True)
            (owned / "data.bin").write_bytes(b"data")
            point = ScanPoint(
                "Darwin 用户缓存",
                (),
                "confirm",
                expand_children=True,
                path_provider="darwin-user-cache",
                process_owner_protection=True,
            )

            with mock.patch.dict("os.environ", {"HOME": str(home)}), mock.patch(
                "openclean.engine.discover_darwin_user_cache",
                return_value=DarwinUserCacheDiscovery(paths=(cache_root,)),
            ), mock.patch(
                "openclean.engine.capture_process_snapshot",
                return_value=ProcessSnapshot(
                    ("/Applications/UURemote.app/Contents/MacOS/UURemote",)
                ),
            ):
                result = scan_points([point], workers=1)

            self.assertEqual(len(result.items), 1)
            item = result.items[0]
            self.assertEqual(item.path, owned)
            self.assertFalse(item.actionable)
            self.assertIn("UURemote.app", item.running_process_markers)
            self.assertIn("正在运行", item.action_block_reason)

    def test_public_xcode_and_ai_points_are_precise_and_process_guarded(self) -> None:
        system = {point.category: point for point in SYSTEM_JUNK}
        developer = {point.category: point for point in DEVELOPER_JUNK}
        ai = {point.category: point for point in AI_TOOL_JUNK}

        self.assertIn("Xcode 文档缓存", system)
        self.assertIn("Xcode 设备日志", system)
        self.assertEqual(system["Xcode Archives"].safety, "critical")
        self.assertTrue(system["Xcode DerivedData"].running_process_markers)
        self.assertTrue(system["Darwin 用户缓存"].process_owner_protection)
        self.assertIn("go test", developer["Go 构建缓存"].running_process_markers)
        self.assertIn("gopls", developer["Go module cache"].running_process_markers)
        self.assertNotIn("~/.codex/cache", ai["Codex 缓存"].paths)
        self.assertIn(
            "~/.codex/cache/codex_apps_tools",
            ai["Codex 缓存"].paths,
        )
        self.assertEqual(
            ai["Codex 缓存"].paths,
            (
                "~/.codex/cache/codex_apps_server_info",
                "~/.codex/cache/codex_apps_tools",
            ),
        )
        self.assertEqual(
            ai["Codex 临时结构与 Crashpad 配对"].scanner,
            "codex-storage-artifacts",
        )
        self.assertIn(
            "~/.claude/stats-cache.json",
            ai["Claude 缓存"].paths,
        )
        self.assertTrue(ai["chrome-devtools-mcp"].running_process_markers)
        self.assertIn(
            "~/.cache/chrome-devtools-mcp/chrome-profile/Default/Cache",
            ai["chrome-devtools-mcp"].paths,
        )


class ProcessProtectedExecutionTests(unittest.TestCase):
    def _scanned_item(self, cache: Path):
        point = ScanPoint(
            "Tool cache",
            (str(cache),),
            running_process_markers=("tool-app",),
        )
        with mock.patch(
            "openclean.engine.capture_process_snapshot",
            return_value=ProcessSnapshot(()),
        ):
            return scan_points([point], workers=1).items[0]

    def test_tool_starting_after_preflight_blocks_final_move(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            cache = home / "cache"
            cache.mkdir(parents=True)
            (cache / "data.bin").write_bytes(b"data")
            item = self._scanned_item(cache)
            calls = 0

            def runner(command, **_):
                nonlocal calls
                calls += 1
                output = "" if calls == 1 else "/Applications/Tool-App"
                return subprocess.CompletedProcess(command, 0, output, "")

            report = execute_cleanup(
                [item],
                IgnoreRules(),
                home=home,
                process_runner=runner,
            )

            self.assertFalse(report.complete)
            self.assertEqual(report.outcomes[0].status, "failed")
            self.assertIn("已启动", report.outcomes[0].message)
            self.assertTrue(cache.exists())
            self.assertFalse((home / ".Trash").exists())

    def test_detection_failure_blocks_batch_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            cache = home / "cache"
            cache.mkdir(parents=True)
            (cache / "data.bin").write_bytes(b"data")
            item = self._scanned_item(cache)

            def runner(command, **_):
                return subprocess.CompletedProcess(command, 1, "", "denied")

            report = execute_cleanup(
                [item],
                IgnoreRules(),
                home=home,
                process_runner=runner,
            )

            self.assertFalse(report.complete)
            self.assertEqual(report.outcomes[0].status, "blocked")
            self.assertIn("进程检测失败", report.outcomes[0].message)
            self.assertTrue(cache.exists())

    def test_stopped_tool_allows_move_after_three_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            cache = home / "cache"
            cache.mkdir(parents=True)
            (cache / "data.bin").write_bytes(b"data")
            item = self._scanned_item(cache)
            runner = mock.Mock(
                side_effect=lambda command, **_: subprocess.CompletedProcess(
                    command, 0, "/usr/bin/other-process", ""
                )
            )

            report = execute_cleanup(
                [item],
                IgnoreRules(),
                home=home,
                process_runner=runner,
            )

            self.assertTrue(report.complete)
            self.assertEqual(report.outcomes[0].status, "moved_to_trash")
            self.assertEqual(runner.call_count, 3)
            self.assertFalse(cache.exists())


if __name__ == "__main__":
    unittest.main()
