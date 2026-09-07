from __future__ import annotations

import contextlib
import io
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openclean.cli import CAT_ART, _run_root_menu, main
from openclean.config import CliConfig, ConfigError, ConfigStore
from openclean.tui import MenuChoice, TUIUnavailable


class _TTYBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


class ConfigStoreTests(unittest.TestCase):
    def test_missing_config_uses_private_default_without_creating_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            store = ConfigStore(path)

            config = store.load()

            self.assertEqual(config, CliConfig(analytics_enabled=False))
            self.assertFalse(path.exists())

    def test_set_is_atomic_private_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "config.json"
            store = ConfigStore(path)

            updated = store.set_analytics(True)

            self.assertTrue(updated.analytics_enabled)
            self.assertEqual(store.load(), updated)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {
                    "schema_version": 1,
                    "analytics_enabled": True,
                },
            )
            self.assertEqual(
                [entry.name for entry in path.parent.iterdir()],
                ["config.json"],
            )

    def test_invalid_existing_config_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text('{"analytics_enabled": "yes"}', encoding="utf-8")
            before = path.read_bytes()

            with self.assertRaisesRegex(ConfigError, "必须是布尔值"):
                ConfigStore(path).set_analytics(False)

            self.assertEqual(path.read_bytes(), before)

    def test_rejects_unknown_fields_and_schema_versions(self) -> None:
        with self.assertRaisesRegex(ConfigError, "未知字段"):
            CliConfig.from_mapping({"unknown": True})
        with self.assertRaisesRegex(ConfigError, "schema_version"):
            CliConfig.from_mapping({"schema_version": 2})


class ConfigAndRootCliTests(unittest.TestCase):
    def test_config_cli_updates_and_reads_back_analytics_preference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                update_status = main(
                    [
                        "config",
                        "--analytics",
                        "on",
                        "--config-path",
                        str(path),
                        "--json",
                    ]
                )
            updated = json.loads(stdout.getvalue())

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                read_status = main(
                    ["config", "--config-path", str(path), "--json"]
                )
            readback = json.loads(stdout.getvalue())

            self.assertEqual(update_status, 0)
            self.assertTrue(updated["changed"])
            self.assertTrue(updated["analytics_enabled"])
            self.assertFalse(updated["analytics_implemented"])
            self.assertEqual(read_status, 0)
            self.assertFalse(readback["changed"])
            self.assertTrue(readback["analytics_enabled"])

    def test_config_cli_rejects_corrupt_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("not-json", encoding="utf-8")
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                status = main(
                    ["config", "--config-path", str(path)]
                )

            self.assertEqual(status, 2)
            self.assertIn("配置操作失败", stderr.getvalue())
            self.assertEqual(path.read_text(encoding="utf-8"), "not-json")

    def test_cat_text_and_json_are_stable(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            text_status = main(["cat"])
        self.assertEqual(text_status, 0)
        self.assertEqual(stdout.getvalue().rstrip("\n"), CAT_ART)

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            json_status = main(["cat", "--json"])
        self.assertEqual(json_status, 0)
        self.assertEqual(json.loads(stdout.getvalue())["cat"], CAT_ART)

    def test_no_subcommand_prints_help_when_not_attached_to_tty(self) -> None:
        stdin = io.StringIO()
        stdout = io.StringIO()

        with mock.patch.object(sys, "stdin", stdin), contextlib.redirect_stdout(stdout):
            status = main([])

        self.assertEqual(status, 0)
        self.assertIn("usage: openclean", stdout.getvalue())
        self.assertIn("config", stdout.getvalue())

    def test_argparse_json_error_uses_stable_envelope(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        with contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            status = main(["scan", "--workers", "0", "--json"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(status, 2)
        self.assertEqual(payload["command"], "scan")
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["exit_code"], 2)
        self.assertEqual(payload["error"]["code"], "usage_error")
        self.assertFalse(payload["executed"])
        self.assertEqual(stderr.getvalue(), "")

    def test_root_menu_handles_invalid_choice_and_dispatches_cat(self) -> None:
        stdin = _TTYBuffer()
        stdout = _TTYBuffer()
        stderr = io.StringIO()

        with mock.patch.object(sys, "stdin", stdin), contextlib.redirect_stdout(
            stdout
        ), contextlib.redirect_stderr(stderr), mock.patch(
            "builtins.input", side_effect=["invalid", "m", "1", "", "q", "q"]
        ), mock.patch(
            "openclean.cli.choose_menu", side_effect=TUIUnavailable("no tty")
        ):
            status = main([])

        self.assertEqual(status, 0)
        self.assertIn("无效选择", stderr.getvalue())
        self.assertIn("openclean", stdout.getvalue())
        self.assertIn("主菜单", stdout.getvalue())
        self.assertIn(CAT_ART, stdout.getvalue())
        self.assertIn("改用行式菜单", stderr.getvalue())

    def test_root_dispatch_preserves_defaults_and_pauses_after_wrapper_returns(self) -> None:
        expected = [["clean"], ["purge"], ["analyze"], ["config"], ["cat"],
                    ["optimize", "ram"], ["optimize", "purgeable"]]
        choices = [MenuChoice(action, 0) for action in
                   ("clean", "purge", "analyze", "config", "more", "cat", "back",
                    "optimize", "ram", "purgeable", "back", "quit")]
        events = []

        def choose(menu, cursor):
            events.append(("menu", menu))
            return choices.pop(0)

        def dispatch(command):
            self.assertEqual(events[-1][0], "menu")
            events.append(("command", command))
            return 0

        def pause(prompt):
            self.assertEqual(events[-1][0], "command")
            events.append(("pause", prompt))
            return ""

        with mock.patch("openclean.cli.choose_menu", side_effect=choose), \
                mock.patch("openclean.cli.main", side_effect=dispatch) as command, \
                mock.patch("builtins.input", side_effect=pause) as wait:
            self.assertEqual(_run_root_menu(), 0)
        self.assertEqual(command.call_args_list, [mock.call(args) for args in expected])
        self.assertEqual(wait.call_count, len(expected))
        self.assertIn(("menu", "more"), events)
        self.assertIn(("menu", "optimize"), events)

    def test_menu_retains_cursors_and_does_not_hide_child_exit_status(self) -> None:
        stderr = io.StringIO()
        with mock.patch("openclean.cli.choose_menu", side_effect=[
            MenuChoice("optimize", 3), MenuChoice("ram", 0), MenuChoice("back", 0),
            MenuChoice("quit", 3),
        ]) as chooser, mock.patch("builtins.input", return_value=""), \
                contextlib.redirect_stderr(stderr):
            self.assertEqual(_run_root_menu(), 0)
        self.assertEqual(chooser.call_args_list[-1], mock.call("root", 3))
        self.assertIn("返回退出码 1", stderr.getvalue())
        self.assertIn("未执行任何操作", stderr.getvalue())

        for status in (2, 130):
            with self.subTest(status=status), \
                    mock.patch("openclean.cli.choose_menu", return_value=MenuChoice("clean", 0)), \
                    mock.patch("openclean.cli.main", return_value=status), \
                    mock.patch("builtins.input") as wait, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(_run_root_menu(), status)
                wait.assert_not_called()

    def test_menu_input_eof_and_interrupt_exit_without_dispatch(self) -> None:
        for error in (EOFError(), KeyboardInterrupt()):
            with self.subTest(error=type(error)), \
                    mock.patch("openclean.cli.choose_menu", side_effect=TUIUnavailable("no tty")), \
                    mock.patch("builtins.input", side_effect=error), \
                    mock.patch("openclean.cli.main") as command, \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(_run_root_menu(), 0)
                command.assert_not_called()

    def test_noninteractive_commands_never_enter_menu_or_wait_for_input(self) -> None:
        for argv in ([], ["cat", "--json"], ["optimize", "ram", "--json"]):
            with self.subTest(argv=argv), \
                    mock.patch.object(sys, "stdin", io.StringIO()), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    mock.patch("openclean.cli.choose_menu") as chooser, \
                    mock.patch("builtins.input") as wait:
                status = main(argv)
                self.assertEqual(status, 1 if "optimize" in argv else 0)
                chooser.assert_not_called()
                wait.assert_not_called()


if __name__ == "__main__":
    unittest.main()
