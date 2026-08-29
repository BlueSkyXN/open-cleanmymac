from __future__ import annotations

import subprocess
import sys
import tomllib
import unittest
from pathlib import Path

from openclean import __version__

IMPLEMENTATION_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = IMPLEMENTATION_ROOT.parent


class PackagingMetadataTests(unittest.TestCase):
    def test_pyproject_declares_installable_console_script(self) -> None:
        with (IMPLEMENTATION_ROOT / "pyproject.toml").open("rb") as stream:
            payload = tomllib.load(stream)

        self.assertEqual(payload["project"]["name"], "open-cleanmymac")
        self.assertEqual(payload["project"]["dynamic"], ["version"])
        self.assertEqual(payload["build-system"]["requires"][0], "setuptools>=77")
        self.assertEqual(payload["project"]["license"], "GPL-3.0-only")
        self.assertEqual(payload["project"]["license-files"], ["LICENSE"])
        self.assertEqual(payload["project"]["requires-python"], ">=3.11")
        self.assertEqual(payload["project"]["dependencies"], [])
        self.assertIn("macos", payload["project"]["keywords"])
        self.assertNotIn(
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
            "include LICENSE",
            "include openclean_cli.py",
            "recursive-include scripts *.py",
            "recursive-include tests *.py",
            "global-exclude .DS_Store",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, manifest)

    def test_package_license_matches_repository_license(self) -> None:
        package_license = (IMPLEMENTATION_ROOT / "LICENSE").read_bytes()
        self.assertIn(b"GNU GENERAL PUBLIC LICENSE", package_license)
        self.assertIn(b"Version 3, 29 June 2007", package_license)

        repository_license = REPOSITORY_ROOT / "LICENSE"
        if repository_license.is_file():
            self.assertEqual(package_license, repository_license.read_bytes())

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
