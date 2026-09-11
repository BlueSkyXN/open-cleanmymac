from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class DeepJsonErrorTests(unittest.TestCase):
    def test_deep_rules_and_config_keep_json_error_contract_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            nested = "[" * 1500 + "0" + "]" * 1500
            rules = home / "rules.json"
            config = home / "config.json"
            rules.write_text('{"schema_version":1,"ignore":{"paths":' + nested + '}}', encoding="utf-8")
            config.write_text('{"schema_version":1,"analytics_enabled":' + nested + '}', encoding="utf-8")
            cases = [(["scan", "--domain", "developer", "--rules", str(rules)], rules, "rules_error")]
            for verb in ("list", "add", "remove"):
                args = ["ignore", verb, "--rules", str(rules)]
                if verb != "list":
                    args.append(str(home / "target"))
                cases.append((args, rules, "rules_error"))
            for flags in ([], ["--analytics", "on"]):
                cases.append((["config", "--config-path", str(config), *flags], config, "config_error"))
            for args, path, error in cases:
                original = path.read_bytes()
                for redact in (False, True):
                    with self.subTest(command=args[:2], redact=redact):
                        result = subprocess.run(
                            [sys.executable, "-m", "openclean.cli", *args, "--json",
                             *(["--redact-paths"] if redact else [])],
                            env={**os.environ, "HOME": str(home), "PYTHONPATH": "."},
                            capture_output=True, text=True, check=False, timeout=10,
                        )
                        self.assertEqual(result.returncode, 2, result.stderr)
                        payload = json.loads(result.stdout)
                        self.assertEqual(payload["error"]["code"], error)
                        self.assertEqual(result.stderr, "")
                        self.assertEqual(path.read_bytes(), original)
                        if redact:
                            self.assertNotIn(str(home), result.stdout)
