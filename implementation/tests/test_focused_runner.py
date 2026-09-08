"""隔离验证 runner；仅 checkout 额外验证真实 Makefile 接线。"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SOURCE_ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = SOURCE_ROOT.parent / "Makefile"


class FocusedRunnerTests(unittest.TestCase):
    failure_exit_code = 1
    empty_pattern_message = "未发现测试"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.implementation = self.root / "implementation"
        self.tests = self.implementation / "tests"
        self.tests.mkdir(parents=True)
        scripts = self.implementation / "scripts"
        scripts.mkdir()
        self.runner = scripts / "run_focused_tests.py"
        shutil.copyfile(SOURCE_ROOT / "scripts/run_focused_tests.py", self.runner)
        (self.tests / "test_pass.py").write_text(
            "import unittest\nclass Passing(unittest.TestCase):\n"
            "    def test_one(self): self.assertTrue(True)\n", encoding="utf-8")
        (self.tests / "test_fail.py").write_text(
            "import unittest\nclass Failing(unittest.TestCase):\n"
            "    def test_one(self): self.fail('synthetic failure')\n", encoding="utf-8")

    def run_pattern(self, pattern):
        return self.run_command(
            [sys.executable, str(self.runner), pattern], cwd=self.implementation)

    def run_command(self, command, *, cwd):
        env = {**os.environ, "HOME": str(self.root)}
        env.pop("MAKEFLAGS", None)
        env.pop("MFLAGS", None)
        return subprocess.run(
            command, cwd=cwd, env=env, capture_output=True, text=True, timeout=30)

    def test_empty_pattern_fails(self):
        result = self.run_pattern("")
        self.assertEqual(result.returncode, 2)
        self.assertIn(self.empty_pattern_message, result.stdout + result.stderr)

    def test_unmatched_pattern_fails(self):
        (self.tests / "test_empty.py").write_text("# 没有测试用例\n", encoding="utf-8")
        for pattern in ("test_missing.py", "test_empty.py"):
            with self.subTest(pattern=pattern):
                result = self.run_pattern(pattern)
                self.assertEqual(result.returncode, 2)
                self.assertIn("未发现测试", result.stdout + result.stderr)

    def test_matching_pass_succeeds(self):
        result = self.run_pattern("test_pass.py")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Ran 1 test", result.stderr)

    def test_matching_failure_is_preserved(self):
        result = self.run_pattern("test_fail.py")
        self.assertEqual(result.returncode, self.failure_exit_code)
        self.assertIn("synthetic failure", result.stderr)
        self.assertIn("Ran 1 test", result.stderr)


@unittest.skipUnless(
    SOURCE_ROOT.name == "implementation" and MAKEFILE.is_file(),
    "仅 checkout 验证 Makefile 接线；sdist 不包含仓库 Makefile",
)
class MakefileFocusedRunnerTests(FocusedRunnerTests):
    failure_exit_code = 2
    empty_pattern_message = "请指定 TEST_PATTERN"

    def setUp(self):
        super().setUp()
        shutil.copyfile(MAKEFILE, self.root / "Makefile")

    def run_pattern(self, pattern):
        return self.run_command(
            ["make", "test-focused", f"PYTHON={sys.executable}", f"TEST_PATTERN={pattern}"],
            cwd=self.root)
