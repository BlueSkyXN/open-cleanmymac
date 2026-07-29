from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

IMPLEMENTATION_ROOT = Path(__file__).resolve().parents[1]


class PreviewAllTests(unittest.TestCase):
    def test_preview_runs_all_scenarios_in_temporary_directory(self) -> None:
        completed = subprocess.run(
            [sys.executable, "scripts/preview_all.py", "--json"],
            cwd=IMPLEMENTATION_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["passed"])
        self.assertFalse(payload["real_user_data_modified"])
        self.assertEqual(payload["workspace"], "TemporaryDirectory")
        self.assertGreaterEqual(payload["scenario_count"], 18)
        self.assertTrue(all(item["passed"] for item in payload["scenarios"]))
        identifiers = {item["identifier"] for item in payload["scenarios"]}
        self.assertIn("scan-all-domains", identifiers)
        self.assertIn("clean-trash-temp-execution", identifiers)
        self.assertIn("purge-temp-execution", identifiers)
        self.assertIn("analyze-temp-execution", identifiers)
        self.assertIn("optimize-ram-guard", identifiers)
        self.assertIn("optimize-purgeable-guard", identifiers)
        statuses = {
            item["capability"]: item["status"]
            for item in payload["guarded_capabilities"]
        }
        self.assertEqual(
            statuses["SMAppService/XPC privileged cleanup"],
            "external-prerequisite",
        )


if __name__ == "__main__":
    unittest.main()
