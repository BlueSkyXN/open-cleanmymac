from __future__ import annotations

import contextlib
import io
import json
import plistlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from openclean.cleanup import execute_cleanup
from openclean.cli import main
from openclean.engine import IgnoreRules, scan_domains
from openclean.knowledge_base import KnowledgeBase
from openclean.scanpoints import DOMAINS, ScanPoint
from openclean.startup_items import (
    StartupItemError,
    StartupProgram,
    UnsupportedStartupItem,
    read_startup_program,
    scan_broken_startup_items,
    startup_program_exists,
)


def _write_plist(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        plistlib.dump(payload, stream)


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
        st_flags=0x40000000,
    )


class StartupProgramTests(unittest.TestCase):
    def test_program_and_program_arguments_follow_launchd_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            absolute = root / "absolute.plist"
            arguments = root / "arguments.plist"
            relative = root / "relative.plist"
            _write_plist(
                absolute,
                {
                    "Program": "/missing/tool",
                    "ProgramArguments": ["ignored"],
                },
            )
            _write_plist(
                arguments,
                {"ProgramArguments": ["/missing/from-arguments", "--flag"]},
            )
            _write_plist(relative, {"ProgramArguments": ["sh", "-c", "true"]})

            self.assertEqual(
                read_startup_program(absolute),
                StartupProgram("/missing/tool"),
            )
            self.assertEqual(
                read_startup_program(arguments),
                StartupProgram("/missing/from-arguments"),
            )
            self.assertEqual(
                read_startup_program(relative),
                StartupProgram("sh", uses_standard_path=True),
            )

    def test_bundle_program_and_ambiguous_relative_path_are_not_guessed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "bundle.plist"
            relative = root / "relative.plist"
            _write_plist(bundle, {"BundleProgram": "Contents/MacOS/helper"})
            _write_plist(relative, {"ProgramArguments": ["tools/helper"]})

            with self.assertRaises(UnsupportedStartupItem):
                read_startup_program(bundle)
            with self.assertRaises(UnsupportedStartupItem):
                read_startup_program(relative)

    def test_invalid_plist_and_nonabsolute_program_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            invalid = root / "invalid.plist"
            nonabsolute = root / "nonabsolute.plist"
            invalid.write_text("not a plist", encoding="utf-8")
            _write_plist(nonabsolute, {"Program": "tool"})

            with self.assertRaises(StartupItemError):
                read_startup_program(invalid)
            with self.assertRaisesRegex(StartupItemError, "绝对路径"):
                read_startup_program(nonabsolute)

    def test_program_existence_supports_absolute_and_standard_path(self) -> None:
        self.assertTrue(startup_program_exists(StartupProgram("/bin/sh")))
        self.assertTrue(
            startup_program_exists(
                StartupProgram("sh", uses_standard_path=True)
            )
        )
        self.assertFalse(
            startup_program_exists(StartupProgram("/definitely/missing/tool"))
        )
        self.assertFalse(
            startup_program_exists(
                StartupProgram(
                    "definitely-missing-openclean-tool",
                    uses_standard_path=True,
                )
            )
        )


class BrokenStartupItemScanTests(unittest.TestCase):
    def test_dataless_plist_is_not_opened(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            agents = home / "Library" / "LaunchAgents"
            plist = agents / "remote.plist"
            _write_plist(plist, {"Program": "/missing/tool"})
            real_stat = plist.lstat()
            original_lstat = Path.lstat

            def fake_lstat(path: Path):
                stat_result = original_lstat(path)
                return _with_dataless_flag(real_stat) if path == plist else stat_result

            with mock.patch(
                "openclean.models.MACOS_SF_DATALESS", 0x40000000
            ), mock.patch.object(Path, "lstat", new=fake_lstat), mock.patch(
                "openclean.startup_items.read_startup_program"
            ) as reader:
                result = scan_broken_startup_items(
                    [agents],
                    IgnoreRules(),
                    home=home,
                )

            reader.assert_not_called()
            self.assertEqual(result.items, [])
            self.assertEqual(result.issues[0].code, "dataless_object_skipped")
            self.assertFalse(result.issues[0].blocking)

    def test_plist_that_becomes_dataless_while_reading_is_not_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            agents = home / "Library" / "LaunchAgents"
            plist = agents / "transition.plist"
            _write_plist(plist, {"Program": "/missing/tool"})
            plist_stat = plist.lstat()
            original_lstat = Path.lstat
            original_reader = read_startup_program
            became_dataless = False

            def fake_lstat(path: Path):
                stat_result = original_lstat(path)
                if path == plist and became_dataless:
                    return _with_dataless_flag(plist_stat)
                return stat_result

            def transitioning_reader(path):
                nonlocal became_dataless
                program = original_reader(path)
                became_dataless = True
                return program

            with mock.patch(
                "openclean.models.MACOS_SF_DATALESS", 0x40000000
            ), mock.patch.object(Path, "lstat", new=fake_lstat), mock.patch(
                "openclean.startup_items.read_startup_program",
                side_effect=transitioning_reader,
            ) as reader:
                result = scan_broken_startup_items(
                    [agents],
                    IgnoreRules(),
                    home=home,
                )

            reader.assert_called_once()
            self.assertEqual(result.items, [])
            self.assertEqual(result.issues[0].code, "dataless_object_skipped")

    def test_reports_only_programs_confirmed_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            agents = home / "Library" / "LaunchAgents"
            broken_absolute = agents / "broken-absolute.plist"
            broken_relative = agents / "broken-relative.plist"
            valid = agents / "valid.plist"
            bundle = agents / "bundle.plist"
            invalid = agents / "invalid.plist"
            _write_plist(
                broken_absolute,
                {"Program": str(Path(tmp) / "missing-tool")},
            )
            _write_plist(
                broken_relative,
                {"ProgramArguments": ["definitely-missing-openclean-tool"]},
            )
            _write_plist(valid, {"Program": "/bin/sh"})
            _write_plist(bundle, {"BundleProgram": "Contents/MacOS/helper"})
            invalid.write_text("invalid", encoding="utf-8")
            (agents / "not-a-plist.txt").write_text("ignored", encoding="utf-8")
            (agents / "linked.plist").symlink_to(broken_absolute)

            result = scan_broken_startup_items(
                [agents],
                IgnoreRules(),
                home=home,
            )

            self.assertTrue(result.complete)
            self.assertEqual(
                {item.path for item in result.items},
                {broken_absolute, broken_relative},
            )
            by_path = {item.path: item for item in result.items}
            self.assertFalse(
                by_path[broken_absolute].startup_program_uses_path
            )
            self.assertTrue(
                by_path[broken_relative].startup_program_uses_path
            )
            self.assertTrue(all(item.safety == "confirm" for item in result.items))
            self.assertTrue(all(item.preselected is False for item in result.items))
            self.assertTrue(all(item.actionable for item in result.items))
            self.assertEqual(
                {issue.code for issue in result.issues},
                {"startup_item_invalid", "startup_item_unverifiable"},
            )
            self.assertTrue(all(not issue.blocking for issue in result.issues))

    def test_knowledge_base_protection_short_circuits_candidate_lstat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            agents = home / "Library" / "LaunchAgents"
            protected = agents / "protected.plist"
            _write_plist(protected, {"Program": "/missing/tool"})
            knowledge_base = KnowledgeBase.from_mapping(
                {
                    "schema_version": 1,
                    "protect": {"paths": [str(protected)]},
                }
            )
            original_lstat = Path.lstat

            def guarded_lstat(path: Path):
                if path == protected:
                    raise AssertionError("受保护启动项不应执行 lstat")
                return original_lstat(path)

            with mock.patch.object(Path, "lstat", new=guarded_lstat):
                result = scan_broken_startup_items(
                    [agents],
                    IgnoreRules(knowledge_base=knowledge_base),
                    home=home,
                )

            self.assertEqual(result.items, [])
            self.assertEqual(result.issues, [])

    def test_system_startup_item_is_visible_but_requires_helper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plist = root / "system.plist"
            _write_plist(plist, {"Program": "/missing/system-tool"})

            with mock.patch(
                "openclean.startup_items.nonprivileged_action_block_reason",
                return_value="系统保护路径",
            ):
                result = scan_broken_startup_items(
                    [root],
                    IgnoreRules(),
                    home=Path(tmp) / "home",
                )

            item = result.items[0]
            self.assertTrue(item.requires_privilege)
            self.assertFalse(item.actionable)
            self.assertIn("特权帮助器", item.action_block_reason)

    def test_engine_dynamic_scanner_assigns_domain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            agents = home / "Library" / "LaunchAgents"
            plist = agents / "broken.plist"
            _write_plist(plist, {"Program": str(Path(tmp) / "missing")})
            point = ScanPoint(
                "失效启动项",
                (str(agents),),
                "confirm",
                scanner="broken-startup-items",
            )

            with mock.patch.dict(DOMAINS, {"system": [point]}):
                result = scan_domains(["system"], workers=1)

            self.assertTrue(result.complete)
            self.assertEqual(len(result.items), 1)
            self.assertEqual(result.items[0].domain, "system")
            self.assertEqual(result.items[0].path, plist)

    def test_clean_json_exposes_revalidation_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            agents = home / "Library" / "LaunchAgents"
            plist = agents / "broken.plist"
            missing = root / "missing"
            rules = root / "rules.json"
            _write_plist(plist, {"Program": str(missing)})
            rules.write_text(
                json.dumps({"schema_version": 1}),
                encoding="utf-8",
            )
            point = ScanPoint(
                "失效启动项",
                (str(agents),),
                "confirm",
                scanner="broken-startup-items",
            )
            stdout = io.StringIO()

            with mock.patch.dict(DOMAINS, {"system": [point]}), \
                    contextlib.redirect_stdout(stdout):
                status = main(
                    ["clean", "junk", "--rules", str(rules), "--json"]
                )

            payload = json.loads(stdout.getvalue())
            item = payload["categories"][0]["items"][0]
            self.assertEqual(status, 0)
            self.assertEqual(item["startup_program"], str(missing))
            self.assertFalse(item["startup_program_uses_path"])
            self.assertFalse(item["preselected"])


class BrokenStartupItemCleanupTests(unittest.TestCase):
    def _scanned_item(self, home: Path, target: Path):
        plist = home / "Library" / "LaunchAgents" / "broken.plist"
        _write_plist(plist, {"Program": str(target)})
        result = scan_broken_startup_items(
            [plist.parent],
            IgnoreRules(),
            home=home,
        )
        self.assertEqual(len(result.items), 1)
        return plist, result.items[0]

    def test_still_broken_user_item_moves_to_trash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            plist, item = self._scanned_item(home, Path(tmp) / "missing")

            report = execute_cleanup([item], IgnoreRules(), home=home)

            self.assertTrue(report.complete)
            self.assertFalse(plist.exists())
            self.assertTrue((home / ".Trash" / plist.name).is_file())

    def test_program_created_after_scan_blocks_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            target = Path(tmp) / "tool"
            home.mkdir()
            plist, item = self._scanned_item(home, target)
            target.write_bytes(b"now available")

            report = execute_cleanup([item], IgnoreRules(), home=home)

            self.assertFalse(report.complete)
            self.assertEqual(report.outcomes[0].status, "blocked")
            self.assertIn("已恢复", report.outcomes[0].message)
            self.assertTrue(plist.is_file())
            self.assertFalse((home / ".Trash").exists())

    def test_plist_becoming_dataless_is_not_read_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            plist, item = self._scanned_item(home, Path(tmp) / "missing")
            plist_stat = plist.lstat()
            original_lstat = Path.lstat
            original_reader = read_startup_program
            became_dataless = False

            def fake_lstat(path: Path):
                stat_result = original_lstat(path)
                if path == plist and became_dataless:
                    return _with_dataless_flag(plist_stat)
                return stat_result

            def transitioning_reader(path):
                nonlocal became_dataless
                program = original_reader(path)
                became_dataless = True
                return program

            with mock.patch(
                "openclean.models.MACOS_SF_DATALESS", 0x40000000
            ), mock.patch.object(Path, "lstat", new=fake_lstat), mock.patch(
                "openclean.startup_items.read_startup_program",
                side_effect=transitioning_reader,
            ) as reader:
                report = execute_cleanup([item], IgnoreRules(), home=home)

            reader.assert_called_once()
            self.assertFalse(report.complete)
            self.assertIn("dataless", report.outcomes[0].message)
            self.assertTrue(plist.is_file())

    def test_program_reference_changed_after_scan_blocks_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            plist, item = self._scanned_item(home, Path(tmp) / "missing-a")
            _write_plist(plist, {"Program": str(Path(tmp) / "missing-b")})

            report = execute_cleanup([item], IgnoreRules(), home=home)

            self.assertFalse(report.complete)
            self.assertEqual(report.outcomes[0].status, "blocked")
            self.assertIn("程序已变化", report.outcomes[0].message)
            self.assertTrue(plist.is_file())

    def test_program_created_during_trash_preparation_blocks_final_move(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            target = Path(tmp) / "tool"
            trash = home / ".Trash"
            home.mkdir()
            plist, item = self._scanned_item(home, target)

            def resolver(_: Path) -> Path:
                trash.mkdir()
                target.write_bytes(b"now available")
                return trash

            report = execute_cleanup(
                [item],
                IgnoreRules(),
                home=home,
                trash_resolver=resolver,
            )

            self.assertFalse(report.complete)
            self.assertEqual(report.outcomes[0].status, "failed")
            self.assertIn("已恢复", report.outcomes[0].message)
            self.assertTrue(plist.is_file())
            self.assertFalse((trash / plist.name).exists())


if __name__ == "__main__":
    unittest.main()
