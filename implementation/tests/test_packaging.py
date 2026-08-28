from __future__ import annotations

import subprocess
import sys
import tomllib
import unittest
from pathlib import Path

from openclean import __version__

IMPLEMENTATION_ROOT = Path(__file__).resolve().parents[1]


class PackagingMetadataTests(unittest.TestCase):
    def test_pyproject_declares_installable_console_script(self) -> None:
        with (IMPLEMENTATION_ROOT / "pyproject.toml").open("rb") as stream:
            payload = tomllib.load(stream)

        self.assertEqual(payload["project"]["name"], "open-cleanmymac")
        self.assertEqual(payload["project"]["dynamic"], ["version"])
        self.assertEqual(payload["project"]["license"], {"text": "GPL-3.0-only"})
        self.assertEqual(payload["project"]["requires-python"], ">=3.11")
        self.assertEqual(payload["project"]["dependencies"], [])
        self.assertIn("macos", payload["project"]["keywords"])
        self.assertIn(
            "License :: OSI Approved :: GNU General Public License v3 (GPLv3)",
            payload["project"]["classifiers"],
        )
        self.assertEqual(
            payload["project"]["urls"]["Repository"],
            "https://github.com/BlueSkyXN/open-cleanmymac",
        )
        self.assertEqual(
            payload["project"]["urls"]["Changelog"],
            "https://github.com/BlueSkyXN/open-cleanmymac/blob/main/CHANGELOG.md",
        )
        self.assertEqual(
            payload["project"]["scripts"]["openclean"],
            "openclean.cli:main",
        )
        self.assertEqual(
            payload["tool"]["setuptools"]["dynamic"]["version"],
            {"attr": "openclean.__version__"},
        )
        self.assertFalse(
            payload["tool"]["setuptools"]["include-package-data"]
        )
        self.assertEqual(
            payload["tool"]["setuptools"]["packages"]["find"]["exclude"],
            ["tests*"],
        )

    def test_sdist_manifest_keeps_self_validation_materials(self) -> None:
        manifest = (IMPLEMENTATION_ROOT / "MANIFEST.in").read_text(
            encoding="utf-8"
        )

        for expected in (
            "include openclean_cli.py",
            "recursive-include scripts *.py",
            "recursive-include tests *.py",
            "global-exclude .DS_Store",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, manifest)

    def test_python_m_openclean_uses_package_entrypoint(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "openclean", "--version"],
            cwd=IMPLEMENTATION_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout.strip(), f"openclean {__version__}")
        self.assertEqual(completed.stderr, "")


if __name__ == "__main__":
    unittest.main()
