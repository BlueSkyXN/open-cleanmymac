"""三种调用方式的模式矩阵：显式模式优先于 TTY 探测，冲突在扫描前失败。"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openclean.cli import main
from openclean.display_mode import DisplayModeError, resolve_display_mode
from openclean.models import Item, ScanResult
from openclean.space_tui import SpaceReviewResult, SpaceTUIUnavailable
from openclean.tui import ReviewResult


def _rules(root: Path) -> Path:
    path = root / "rules.json"
    path.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    return path


def _run_main(
    argv: list[str],
    *,
    stdin_tty: bool = False,
    stdout_tty: bool = False,
) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
            mock.patch("sys.stdin.isatty", return_value=stdin_tty), \
            mock.patch("sys.stdout.isatty", return_value=stdout_tty):
        status = main(argv)
    return status, stdout.getvalue(), stderr.getvalue()


class ResolveDisplayModeTests(unittest.TestCase):
    def test_explicit_flags_are_mutually_exclusive(self) -> None:
        with self.assertRaises(DisplayModeError) as raised:
            resolve_display_mode(
                json_output=False,
                interactive=True,
                no_interactive=True,
                stdin_isatty=True,
                stdout_isatty=True,
            )
        self.assertEqual(raised.exception.code, "invalid_mode_options")

    def test_json_conflicts_with_explicit_interactive_modes(self) -> None:
        for interactive, line in ((True, False), (False, True)):
            with self.subTest(interactive=interactive, line=line):
                with self.assertRaises(DisplayModeError):
                    resolve_display_mode(
                        json_output=True,
                        interactive=interactive,
                        line_interactive=line,
                        stdin_isatty=True,
                        stdout_isatty=True,
                    )

    def test_json_with_no_interactive_is_redundant_but_allowed(self) -> None:
        self.assertEqual(
            resolve_display_mode(
                json_output=True,
                no_interactive=True,
                stdin_isatty=True,
                stdout_isatty=True,
            ),
            "text",
        )

    def test_explicit_interactive_requires_both_streams_terminal(self) -> None:
        with self.assertRaises(DisplayModeError) as raised:
            resolve_display_mode(
                json_output=False,
                interactive=True,
                stdin_isatty=True,
                stdout_isatty=False,
            )
        self.assertEqual(raised.exception.code, "interactive_requires_terminal")

    def test_explicit_interactive_rejects_parameterized_selection(self) -> None:
        with self.assertRaises(DisplayModeError):
            resolve_display_mode(
                json_output=False,
                interactive=True,
                parameterized_selection=True,
                stdin_isatty=True,
                stdout_isatty=True,
            )

    def test_auto_routing_keeps_compat_default(self) -> None:
        self.assertEqual(
            resolve_display_mode(
                json_output=False,
                stdin_isatty=True,
                stdout_isatty=True,
            ),
            "tui",
        )
        self.assertEqual(
            resolve_display_mode(
                json_output=False,
                parameterized_selection=True,
                stdin_isatty=True,
                stdout_isatty=True,
            ),
            "text",
        )
        self.assertEqual(
            resolve_display_mode(
                json_output=False,
                stdin_isatty=True,
                stdout_isatty=False,
            ),
            "text",
        )

    def test_line_interactive_and_no_interactive_are_explicit(self) -> None:
        self.assertEqual(
            resolve_display_mode(
                json_output=False,
                line_interactive=True,
                stdin_isatty=True,
                stdout_isatty=True,
            ),
            "line",
        )
        self.assertEqual(
            resolve_display_mode(
                json_output=False,
                no_interactive=True,
                stdin_isatty=True,
                stdout_isatty=True,
            ),
            "text",
        )


class AnalyzeModeContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / "data").mkdir()
        self.rules = _rules(self.root)

    def test_json_wins_over_pty_allocated_by_caller(self) -> None:
        with mock.patch("openclean.cli.review_space") as review:
            status, stdout, _ = _run_main(
                ["analyze", str(self.root / "data"), "--json", "--rules", str(self.rules)],
                stdin_tty=True,
                stdout_tty=True,
            )
        payload = json.loads(stdout)
        self.assertEqual(status, 0)
        self.assertEqual(payload["command"], "analyze")
        review.assert_not_called()

    def test_closed_stdin_stays_text_even_with_tty_stdout(self) -> None:
        with mock.patch("openclean.cli.review_space") as review:
            status, stdout, _ = _run_main(
                ["analyze", str(self.root / "data"), "--rules", str(self.rules)],
                stdin_tty=False,
                stdout_tty=True,
            )
        self.assertEqual(status, 0)
        self.assertIn("空间分析", stdout)
        review.assert_not_called()

    def test_interactive_with_json_fails_before_scan(self) -> None:
        with mock.patch("openclean.cli.analyze_path") as scanner, mock.patch(
            "openclean.cli.review_space"
        ) as review:
            status, stdout, _ = _run_main(
                ["analyze", str(self.root / "data"), "--interactive", "--json"],
                stdin_tty=True,
                stdout_tty=True,
            )
        payload = json.loads(stdout)
        self.assertEqual(status, 2)
        self.assertEqual(payload["error"]["code"], "invalid_mode_options")
        scanner.assert_not_called()
        review.assert_not_called()

    def test_interactive_without_terminal_fails_before_scan(self) -> None:
        with mock.patch("openclean.cli.analyze_path") as scanner:
            status, _, stderr = _run_main(
                ["analyze", str(self.root / "data"), "--interactive"],
                stdin_tty=False,
                stdout_tty=False,
            )
        self.assertEqual(status, 2)
        self.assertIn("--interactive", stderr)
        scanner.assert_not_called()

    def test_interactive_rejects_parameterized_selection(self) -> None:
        with mock.patch("openclean.cli.analyze_path") as scanner:
            status, _, stderr = _run_main(
                [
                    "analyze", str(self.root / "data"),
                    "--interactive", "--select", str(self.root / "data"),
                ],
                stdin_tty=True,
                stdout_tty=True,
            )
        self.assertEqual(status, 2)
        self.assertIn("--interactive", stderr)
        scanner.assert_not_called()

    def test_interactive_reaches_tui_and_does_not_fallback_on_failure(self) -> None:
        with mock.patch(
            "openclean.cli.review_space",
            side_effect=SpaceTUIUnavailable("no terminal"),
        ) as review, mock.patch("openclean.cli.run_space_browser") as line_browser:
            status, _, stderr = _run_main(
                ["analyze", str(self.root / "data"), "--interactive"],
                stdin_tty=True,
                stdout_tty=True,
            )
        self.assertEqual(status, 2)
        self.assertIn("显式 --interactive", stderr)
        review.assert_called_once()
        line_browser.assert_not_called()

    def test_interactive_routes_to_tui_on_terminal(self) -> None:
        with mock.patch(
            "openclean.cli.review_space",
            return_value=SpaceReviewResult((), False, False, True),
        ) as review:
            status, _, _ = _run_main(
                ["analyze", str(self.root / "data"), "--interactive"],
                stdin_tty=True,
                stdout_tty=True,
            )
        self.assertEqual(status, 0)
        review.assert_called_once()

    def test_conflicting_explicit_modes_are_usage_errors(self) -> None:
        status, _, stderr = _run_main(
            ["analyze", str(self.root / "data"), "--interactive", "--no-interactive"],
        )
        self.assertEqual(status, 2)
        self.assertIn("error", stderr)

    def test_line_interactive_conflicts_reported_before_scan(self) -> None:
        with mock.patch("openclean.cli.run_space_browser") as browser, mock.patch(
            "openclean.cli.analyze_path"
        ) as scanner:
            status, stdout, _ = _run_main(
                ["analyze", str(self.root / "data"), "--line-interactive", "--json"],
                stdin_tty=True,
                stdout_tty=True,
            )
        payload = json.loads(stdout)
        self.assertEqual(status, 2)
        self.assertEqual(payload["error"]["code"], "invalid_mode_options")
        browser.assert_not_called()
        scanner.assert_not_called()


class CleanupModeContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.rules = _rules(self.root)

    def _result(self) -> ScanResult:
        item = Item(
            self.root / "cache",
            100,
            category="环境缓存",
            safety="safe",
            domain="developer",
        )
        return ScanResult(items=[item])

    def test_clean_interactive_conflicts_fail_before_scan(self) -> None:
        for argv, json_mode in (
            (["clean", "--interactive", "--select", str(self.root / "cache")], False),
            (["clean", "--interactive", "--json"], True),
            (["clean", "--interactive", "--run", "r1", "--finding", "f1"], False),
        ):
            with self.subTest(argv=argv):
                with mock.patch("openclean.cli.scan_domains") as scanner:
                    status, stdout, stderr = _run_main(
                        [*argv, "--rules", str(self.rules)],
                        stdin_tty=True, stdout_tty=True,
                    )
                self.assertEqual(status, 2)
                if json_mode:
                    payload = json.loads(stdout)
                    self.assertEqual(payload["error"]["code"], "invalid_mode_options")
                else:
                    self.assertIn("--interactive", stderr)
                scanner.assert_not_called()

    def test_clean_interactive_without_terminal_fails(self) -> None:
        with mock.patch("openclean.cli.scan_domains") as scanner:
            status, _, stderr = _run_main(
                ["clean", "--interactive", "--rules", str(self.rules)],
                stdin_tty=True,
                stdout_tty=False,
            )
        self.assertEqual(status, 2)
        self.assertIn("--interactive", stderr)
        scanner.assert_not_called()

    def test_clean_interactive_reaches_tui_review(self) -> None:
        from openclean.scan_tui import ScanScreenOutcome

        with mock.patch(
            "openclean.cli.scan_domains", return_value=self._result()
        ) as scanner, mock.patch(
            "openclean.cli.start_scan_screen",
            return_value=ScanScreenOutcome("done", result=self._result()),
        ) as scan_screen, mock.patch(
            "openclean.cli.review_cleanup",
            return_value=ReviewResult((), False, False, True),
        ) as review:
            status, _, _ = _run_main(
                ["clean", "--interactive", "--rules", str(self.rules)],
                stdin_tty=True,
                stdout_tty=True,
            )
        self.assertEqual(status, 0)
        scanner.assert_not_called()
        scan_screen.assert_called_once()
        review.assert_called_once()

    def test_clean_interactive_tui_failure_does_not_fallback(self) -> None:
        with mock.patch(
            "openclean.cli.scan_domains", return_value=self._result()
        ) as scanner, mock.patch(
            "openclean.cli.review_cleanup"
        ) as review:
            status, _, stderr = _run_main(
                ["clean", "--interactive", "--rules", str(self.rules)],
                stdin_tty=True,
                stdout_tty=True,
            )
        self.assertEqual(status, 2)
        self.assertIn("显式 --interactive", stderr)
        scanner.assert_not_called()
        review.assert_not_called()

    def test_purge_interactive_conflicts_fail_before_scan(self) -> None:
        with mock.patch("openclean.cli.scan_project_artifacts") as scanner:
            status, stdout, _ = _run_main(
                ["purge", "--interactive", "--json", "--rules", str(self.rules)],
                stdin_tty=True,
                stdout_tty=True,
            )
        payload = json.loads(stdout)
        self.assertEqual(status, 2)
        self.assertEqual(payload["error"]["code"], "invalid_mode_options")
        scanner.assert_not_called()

    def test_clean_json_with_pty_stays_programmatic(self) -> None:
        with mock.patch(
            "openclean.cli.scan_domains", return_value=ScanResult()
        ) as scanner, mock.patch("openclean.cli.review_cleanup") as review:
            status, stdout, _ = _run_main(
                ["clean", "--json", "--rules", str(self.rules)],
                stdin_tty=True,
                stdout_tty=False,
            )
        payload = json.loads(stdout)
        self.assertEqual(status, 0)
        self.assertEqual(payload["command"], "clean")
        scanner.assert_called_once()
        review.assert_not_called()


if __name__ == "__main__":
    unittest.main()
