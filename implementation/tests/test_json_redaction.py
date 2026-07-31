from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from openclean.cli import main
from openclean.models import Item, ScanIssue, ScanResult
from openclean.scanpoints import DOMAINS, ScanPoint


class JsonPathRedactionTests(unittest.TestCase):
    def test_scan_redacts_structured_and_embedded_paths_with_stable_refs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "Private Project"
            candidate = project / "cache.bin"
            project.mkdir()
            candidate.write_bytes(b"data")
            rules = root / "rules.json"
            rules.write_text(
                json.dumps({"schema_version": 1}), encoding="utf-8"
            )
            item = Item(
                path=candidate,
                size=4,
                category="Synthetic",
                project_root=project,
                startup_program=str(candidate),
                note=f"candidate path: {candidate}",
            )
            stdout = io.StringIO()

            with mock.patch(
                "openclean.cli.scan_domains",
                return_value=ScanResult(items=[item]),
            ), contextlib.redirect_stdout(stdout):
                status = main(
                    [
                        "scan",
                        "--domain",
                        "developer",
                        "--rules",
                        str(rules),
                        "--json",
                        "--redact-paths",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            serialized = json.dumps(payload, ensure_ascii=False)
            redacted_item = payload["items"][0]
            self.assertEqual(status, 0)
            self.assertEqual(
                payload["redaction"],
                {
                    "enabled": True,
                    "scheme": "opaque-path-ref-v1",
                    "scope": "single-document",
                    "selection_replayable": False,
                },
            )
            self.assertNotIn(str(root), serialized)
            self.assertRegex(redacted_item["path"], r"^path:\d{4}$")
            self.assertEqual(
                redacted_item["startup_program"], redacted_item["path"]
            )
            self.assertIn(redacted_item["path"], redacted_item["note"])
            self.assertNotEqual(
                redacted_item["project_root"], redacted_item["path"]
            )

    def test_default_json_keeps_exact_paths_for_selector_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate = root / "cache.bin"
            candidate.write_bytes(b"data")
            rules = root / "rules.json"
            rules.write_text(
                json.dumps({"schema_version": 1}), encoding="utf-8"
            )
            stdout = io.StringIO()

            with mock.patch(
                "openclean.cli.scan_domains",
                return_value=ScanResult(
                    items=[Item(path=candidate, size=4, category="Synthetic")]
                ),
            ), contextlib.redirect_stdout(stdout):
                status = main(
                    [
                        "scan",
                        "--domain",
                        "developer",
                        "--rules",
                        str(rules),
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(status, 0)
            self.assertEqual(payload["items"][0]["path"], str(candidate))
            self.assertNotIn("redaction", payload)

    def test_usage_error_redacts_path_before_argparse_builds_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            secret = str(Path(tmp) / "private-input")
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                status = main(
                    [
                        "scan",
                        "--json",
                        "--redact-paths",
                        "--not-a-real-option",
                        secret,
                    ]
                )

            payload = json.loads(stdout.getvalue())
            serialized = json.dumps(payload, ensure_ascii=False)
            self.assertEqual(status, 2)
            self.assertEqual(payload["error"]["code"], "usage_error")
            self.assertFalse(payload["executed"])
            self.assertNotIn(tmp, serialized)
            self.assertIn("redaction", payload)

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            status = main(["cat", "--redact-paths"])
        self.assertEqual(status, 2)
        self.assertIn("必须与 --json 同时使用", stderr.getvalue())

        for abbreviated in ("--js", "--redact-p"):
            with self.subTest(abbreviated=abbreviated):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    status = main(["cat", abbreviated])
                self.assertEqual(status, 2)
                self.assertIn("unrecognized arguments", stderr.getvalue())

    def test_clean_execution_redacts_audit_paths_without_changing_target(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            cache = home / "Library" / "Caches" / "private-tool"
            cache.mkdir(parents=True)
            (cache / "data.bin").write_bytes(b"data")
            rules = root / "rules.json"
            rules.write_text(
                json.dumps({"schema_version": 1}), encoding="utf-8"
            )
            stdout = io.StringIO()

            def rename(
                source_parent_fd: int,
                source_name: str,
                destination_parent_fd: int,
                destination_name: str,
            ) -> None:
                os.rename(
                    source_name,
                    destination_name,
                    src_dir_fd=source_parent_fd,
                    dst_dir_fd=destination_parent_fd,
                )

            with mock.patch.dict(os.environ, {"HOME": str(home)}), mock.patch.dict(
                DOMAINS,
                {
                    "developer": [
                        ScanPoint("Synthetic", (str(cache),), domain="developer")
                    ]
                },
            ), mock.patch(
                "openclean.cleanup._rename_no_replace", side_effect=rename
            ), contextlib.redirect_stdout(stdout):
                status = main(
                    [
                        "clean",
                        "dev",
                        "--select",
                        str(cache),
                        "--yes",
                        "--no-interactive",
                        "--rules",
                        str(rules),
                        "--json",
                        "--redact-paths",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            serialized = json.dumps(payload, ensure_ascii=False)
            outcome = payload["cleanup"]["outcomes"][0]
            item = payload["categories"][0]["items"][0]
            self.assertEqual(status, 0)
            self.assertFalse(cache.exists())
            self.assertTrue((home / ".Trash" / cache.name).is_dir())
            self.assertEqual(outcome["status"], "moved_to_trash")
            self.assertEqual(item["path"], outcome["path"])
            self.assertRegex(outcome["destination"], r"^path:\d{4}$")
            self.assertNotIn(str(root), serialized)

    def test_ignore_config_and_knowledge_readbacks_redact_only_file_paths(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ignored = root / "private-cache"
            rules = root / "rules.json"
            config = root / "config.json"
            destination = root / "knowledge.json"
            public_key = root / "public.pem"
            public_key.write_text("test key", encoding="utf-8")

            ignore_stdout = io.StringIO()
            with contextlib.redirect_stdout(ignore_stdout):
                ignore_status = main(
                    [
                        "ignore",
                        "add",
                        str(ignored),
                        "--rules",
                        str(rules),
                        "--json",
                        "--redact-paths",
                    ]
                )

            config_stdout = io.StringIO()
            with contextlib.redirect_stdout(config_stdout):
                config_status = main(
                    [
                        "config",
                        "--analytics",
                        "off",
                        "--config-path",
                        str(config),
                        "--json",
                        "--redact-paths",
                    ]
                )

            update_stdout = io.StringIO()
            source_url = "https://updates.example.test/knowledge.json"
            update = SimpleNamespace(
                destination=destination,
                sequence=1,
                source_url=source_url,
                public_key_sha256="a" * 64,
                rules_sha256="b" * 64,
            )
            with mock.patch(
                "openclean.cli.update_knowledge_base", return_value=update
            ), contextlib.redirect_stdout(update_stdout):
                update_status = main(
                    [
                        "config",
                        "--update-knowledge",
                        source_url,
                        "--knowledge-public-key",
                        str(public_key),
                        "--knowledge-path",
                        str(destination),
                        "--json",
                        "--redact-paths",
                    ]
                )

            payloads = [
                json.loads(ignore_stdout.getvalue()),
                json.loads(config_stdout.getvalue()),
                json.loads(update_stdout.getvalue()),
            ]
            serialized = json.dumps(payloads, ensure_ascii=False)
            self.assertEqual((ignore_status, config_status, update_status), (0, 0, 0))
            self.assertNotIn(str(root), serialized)
            self.assertRegex(payloads[0]["rules_path"], r"^path:\d{4}$")
            self.assertRegex(payloads[0]["paths"][0], r"^path:\d{4}$")
            self.assertRegex(payloads[1]["config_path"], r"^path:\d{4}$")
            self.assertRegex(payloads[2]["destination"], r"^path:\d{4}$")
            self.assertEqual(payloads[2]["source_url"], source_url)

    def test_unstructured_issue_path_is_hidden_while_code_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rules = root / "rules.json"
            rules.write_text(
                json.dumps({"schema_version": 1}), encoding="utf-8"
            )
            secret = root / "unlisted" / "private.log"
            stdout = io.StringIO()

            with mock.patch(
                "openclean.cli.scan_domains",
                return_value=ScanResult(
                    issues=[
                        ScanIssue(
                            code="synthetic_failure",
                            message=f"cannot inspect,{secret}",
                            task="synthetic",
                        ),
                        ScanIssue(
                            code="tilde_path",
                            message="cannot inspect ~/private.log",
                            task="synthetic",
                        ),
                        ScanIssue(
                            code="path_uri",
                            message=f"cannot connect unix://{root}/private.sock",
                            task="synthetic",
                        ),
                    ]
                ),
            ), contextlib.chdir(root), contextlib.redirect_stdout(stdout):
                status = main(
                    [
                        "scan",
                        "--domain",
                        "developer",
                        "--rules",
                        str(rules),
                        "--json",
                        "--redact-paths",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(status, 1)
            self.assertEqual(payload["issues"][0]["code"], "synthetic_failure")
            self.assertTrue(all(
                issue["message"] == "[path details redacted]"
                for issue in payload["issues"]
            ))
            serialized = json.dumps(payload, ensure_ascii=False)
            self.assertNotIn(str(root), serialized)
            self.assertNotRegex(serialized, r"path:\d+/")


if __name__ == "__main__":
    unittest.main()
