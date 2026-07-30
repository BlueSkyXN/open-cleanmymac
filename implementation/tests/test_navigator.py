from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openclean.cli import main
from openclean.engine import IgnoreRules
from openclean.navigator import RevealError, reveal_in_finder, run_space_browser
from openclean.space_tui import SpaceReviewResult


class _TTYBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


class SpaceBrowserTests(unittest.TestCase):
    def test_top_limit_is_visible_in_line_browser(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            root.mkdir()
            (root / "large.bin").write_bytes(b"x" * 10_000)
            (root / "small.bin").write_bytes(b"x")
            output = io.StringIO()

            status = run_space_browser(
                root,
                protection=IgnoreRules(),
                top=1,
                input_fn=lambda _: "q",
                output=output,
                error=io.StringIO(),
            )

            self.assertEqual(status, 0)
            self.assertIn("仅显示最大的 1/2 项", output.getvalue())
            self.assertIn("容量与百分比基于", output.getvalue())

    def test_navigates_into_directory_and_back_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            child = root / "large-directory"
            child.mkdir(parents=True)
            nested = child / "nested.bin"
            sibling = root / "small.bin"
            nested.write_bytes(b"n" * 20_000)
            sibling.write_bytes(b"small")
            commands = iter(["1", "..", "q"])
            output = io.StringIO()
            error = io.StringIO()

            status = run_space_browser(
                root,
                protection=IgnoreRules(),
                input_fn=lambda _: next(commands),
                output=output,
                error=error,
            )

            self.assertEqual(status, 0)
            self.assertGreaterEqual(output.getvalue().count(str(root)), 2)
            self.assertIn(f"空间浏览：{child}", output.getvalue())
            self.assertEqual(nested.read_bytes(), b"n" * 20_000)
            self.assertEqual(sibling.read_bytes(), b"small")
            self.assertEqual(error.getvalue(), "")

    def test_reveal_is_only_called_for_explicit_o_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            root.mkdir()
            selected = root / "file.bin"
            selected.write_bytes(b"data")
            commands = iter(["o 1", "q"])
            revealed: list[Path] = []
            output = io.StringIO()

            status = run_space_browser(
                root,
                protection=IgnoreRules(),
                input_fn=lambda _: next(commands),
                output=output,
                error=io.StringIO(),
                revealer=revealed.append,
            )

            self.assertEqual(status, 0)
            self.assertEqual(revealed, [selected])
            self.assertIn("已在 Finder 中显示", output.getvalue())
            self.assertTrue(selected.exists())

    def test_invalid_commands_and_file_navigation_are_non_destructive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            root.mkdir()
            selected = root / "file.bin"
            selected.write_bytes(b"data")
            commands = iter(["bad", "9", "1", "..", "q"])
            error = io.StringIO()

            status = run_space_browser(
                root,
                protection=IgnoreRules(),
                input_fn=lambda _: next(commands),
                output=io.StringIO(),
                error=error,
            )

            self.assertEqual(status, 0)
            self.assertIn("无法识别命令", error.getvalue())
            self.assertIn("编号超出范围", error.getvalue())
            self.assertIn("不是可进入的目录", error.getvalue())
            self.assertIn("已经位于起始目录", error.getvalue())
            self.assertEqual(selected.read_bytes(), b"data")

    def test_reveal_adapter_uses_open_dash_r_and_reports_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "file.bin"
            path.write_bytes(b"data")
            calls: list[tuple[list[str], dict[str, object]]] = []

            def successful(command, **kwargs):
                calls.append((command, kwargs))
                return subprocess.CompletedProcess(command, 0, "", "")

            reveal_in_finder(path, runner=successful)

            self.assertEqual(calls[0][0], ["/usr/bin/open", "-R", str(path)])
            self.assertTrue(calls[0][1]["capture_output"])

            with self.assertRaisesRegex(RevealError, "Finder failed"):
                reveal_in_finder(
                    path,
                    runner=lambda command, **_: subprocess.CompletedProcess(
                        command, 1, "", "Finder failed"
                    ),
                )

    def test_reveal_rejects_missing_and_symlink_paths_before_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target.bin"
            link = root / "link.bin"
            target.write_bytes(b"data")
            link.symlink_to(target)
            runner = mock.Mock()

            with self.assertRaisesRegex(RevealError, "符号链接"):
                reveal_in_finder(link, runner=runner)
            with self.assertRaisesRegex(RevealError, "不存在"):
                reveal_in_finder(root / "missing", runner=runner)
            runner.assert_not_called()


class AnalyzeTtyRoutingTests(unittest.TestCase):
    def test_tty_text_mode_dispatches_to_full_screen_browser(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            root.mkdir()
            rules = Path(tmp) / "rules.json"
            rules.write_text(
                json.dumps({"schema_version": 1}), encoding="utf-8"
            )
            stdin = _TTYBuffer()
            stdout = _TTYBuffer()

            with mock.patch.object(sys, "stdin", stdin), contextlib.redirect_stdout(
                stdout
            ), mock.patch(
                "openclean.cli.review_space",
                return_value=SpaceReviewResult((), False, False, True),
            ) as browser:
                status = main(
                    ["analyze", str(root), "--rules", str(rules)]
                )

            self.assertEqual(status, 0)
            browser.assert_called_once()
            self.assertEqual(browser.call_args.args[0], root)

    def test_line_interactive_explicitly_uses_compatibility_browser(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            root.mkdir()
            rules = Path(tmp) / "rules.json"
            rules.write_text(
                json.dumps({"schema_version": 1}), encoding="utf-8"
            )

            with mock.patch(
                "openclean.cli.run_space_browser", return_value=0
            ) as browser:
                status = main(
                    [
                        "analyze",
                        str(root),
                        "--rules",
                        str(rules),
                        "--line-interactive",
                    ]
                )

            self.assertEqual(status, 0)
            browser.assert_called_once()

    def test_json_and_no_interactive_modes_never_start_browser(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            root.mkdir()
            (root / "file.bin").write_bytes(b"data")
            rules = Path(tmp) / "rules.json"
            rules.write_text(
                json.dumps({"schema_version": 1}), encoding="utf-8"
            )
            stdin = _TTYBuffer()

            for extra in (["--json"], ["--no-interactive"]):
                stdout = _TTYBuffer()
                with mock.patch.object(sys, "stdin", stdin), \
                        contextlib.redirect_stdout(stdout), mock.patch(
                            "openclean.cli.review_space"
                        ) as browser, mock.patch(
                            "openclean.cli.run_space_browser"
                        ) as line_browser:
                    status = main(
                        ["analyze", str(root), "--rules", str(rules), *extra]
                    )
                self.assertEqual(status, 0)
                browser.assert_not_called()
                line_browser.assert_not_called()


if __name__ == "__main__":
    unittest.main()
