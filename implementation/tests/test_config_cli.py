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

from openclean.cli import CAT_ART, main
from openclean.config import CliConfig, ConfigError, ConfigStore


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
            "builtins.input", side_effect=["invalid", "5", "q"]
        ):
            status = main([])

        self.assertEqual(status, 0)
        self.assertIn("无效选择", stderr.getvalue())
        self.assertIn("openclean", stdout.getvalue())
        self.assertIn("Quick actions", stdout.getvalue())
        self.assertIn(CAT_ART, stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
