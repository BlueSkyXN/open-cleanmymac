from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path

from openclean.cli import main

IMPLEMENTATION_ROOT = Path(__file__).resolve().parents[1]


class OptimizeCommandTests(unittest.TestCase):
    def test_ram_surface_refuses_without_running_an_executor(self) -> None:
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            status = main(["optimize", "ram"])

        self.assertEqual(status, 1)
        self.assertIn("没有已验证的公开无特权", stderr.getvalue())
        self.assertIn("未执行任何操作", stderr.getvalue())

    def test_purgeable_surface_refuses_without_claiming_success(self) -> None:
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            status = main(["optimize", "purgeable"])

        self.assertEqual(status, 1)
        self.assertIn("尚未找到可验证且安全", stderr.getvalue())
        self.assertIn("未执行任何操作", stderr.getvalue())

    def test_json_surface_reports_guarded_unavailable_state(self) -> None:
        for target in ("ram", "purgeable"):
            with self.subTest(target=target):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with contextlib.redirect_stdout(stdout), \
                        contextlib.redirect_stderr(stderr):
                    status = main(["optimize", target, "--json"])

                payload = json.loads(stdout.getvalue())
                self.assertEqual(status, 1)
                self.assertEqual(payload["command"], f"optimize {target}")
                self.assertEqual(payload["status"], "unavailable")
                self.assertFalse(payload["executed"])
                self.assertTrue(payload["reason"])
                self.assertEqual(stderr.getvalue(), "")

    def test_installed_help_shape_lists_public_optimize_subcommands(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "openclean", "optimize", "--help"],
            cwd=IMPLEMENTATION_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(completed.returncode, 0)
        self.assertIn("{ram,purgeable}", completed.stdout)
        self.assertIn("ram", completed.stdout)
        self.assertIn("purgeable", completed.stdout)


if __name__ == "__main__":
    unittest.main()
