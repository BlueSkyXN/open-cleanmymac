from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openclean.cli import main
from openclean.scanpoints import DEVELOPER_JUNK, DOMAINS, ScanPoint


class CleanPreviewTests(unittest.TestCase):
    def test_verified_additional_developer_cache_paths_are_present(self) -> None:
        by_category = {point.category: point.paths for point in DEVELOPER_JUNK}
        self.assertEqual(
            by_category["Poetry 缓存"], ("~/Library/Caches/pypoetry",)
        )
        self.assertEqual(
            by_category["uv 缓存"], ("~/.cache/uv",)
        )
        self.assertEqual(
            by_category["Bun 缓存"], ("~/.bun/install/cache",)
        )
        self.assertEqual(
            by_category["Maven 本地仓库"], ("~/.m2/repository",)
        )

    def test_poetry_and_uv_cache_environment_overrides_are_scanned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            poetry_cache = home / "Library" / "Caches" / "poetry-custom"
            uv_cache = home / ".cache" / "uv-custom"
            poetry_cache.mkdir(parents=True)
            uv_cache.mkdir(parents=True)
            (poetry_cache / "data.bin").write_bytes(b"poetry")
            (uv_cache / "data.bin").write_bytes(b"uv")
            rules = root / "rules.json"
            rules.write_text(
                json.dumps({"schema_version": 1}), encoding="utf-8"
            )
            points = [
                ScanPoint(
                    "Poetry 缓存", (), env_paths=("POETRY_CACHE_DIR",)
                ),
                ScanPoint("uv 缓存", (), env_paths=("UV_CACHE_DIR",)),
            ]
            stdout = io.StringIO()

            with mock.patch.dict(DOMAINS, {"developer": points}), \
                    mock.patch.dict(
                        "os.environ",
                        {
                            "HOME": str(home),
                            "POETRY_CACHE_DIR": str(poetry_cache),
                            "UV_CACHE_DIR": str(uv_cache),
                        },
                        clear=False,
                    ), contextlib.redirect_stdout(stdout):
                status = main(
                    ["clean", "dev", "--rules", str(rules), "--json"]
                )

            payload = json.loads(stdout.getvalue())
            items = payload["categories"][0]["items"]
            self.assertEqual(status, 0)
            self.assertEqual(
                {item["path"] for item in items},
                {str(poetry_cache), str(uv_cache)},
            )
            self.assertTrue(all(item["safety"] == "confirm" for item in items))
            self.assertTrue(all(not item["preselected"] for item in items))
            self.assertTrue(
                all(item["requires_explicit_selection"] for item in items)
            )
            self.assertTrue(
                all(item["path_source"] == "environment" for item in items)
            )

    def test_unsafe_environment_override_is_rejected_before_scanning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            documents = home / "Documents"
            documents.mkdir(parents=True)
            important = documents / "important.txt"
            important.write_text("keep", encoding="utf-8")
            rules = root / "rules.json"
            rules.write_text(
                json.dumps({"schema_version": 1}), encoding="utf-8"
            )
            points = [
                ScanPoint("uv 缓存", (), env_paths=("UV_CACHE_DIR",)),
            ]
            stdout = io.StringIO()

            with mock.patch.dict(DOMAINS, {"developer": points}), mock.patch.dict(
                "os.environ",
                {"HOME": str(home), "UV_CACHE_DIR": str(documents)},
                clear=False,
            ), contextlib.redirect_stdout(stdout):
                status = main(
                    [
                        "clean",
                        "dev",
                        "--force",
                        "--yes",
                        "--rules",
                        str(rules),
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(status, 1)
            self.assertEqual(payload["categories"], [])
            self.assertEqual(
                payload["issues"][0]["code"],
                "unsafe_environment_path",
            )
            self.assertTrue(important.is_file())
            self.assertFalse((home / ".Trash").exists())

    def test_clean_dev_emits_classified_preview_and_default_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            safe_cache = root / "safe-cache"
            confirm_cache = root / "confirm-cache"
            safe_cache.mkdir()
            confirm_cache.mkdir()
            (safe_cache / "data.bin").write_bytes(b"safe")
            (confirm_cache / "data.bin").write_bytes(b"confirm")
            rules = root / "rules.json"
            rules.write_text(
                json.dumps({"schema_version": 1}), encoding="utf-8"
            )
            points = [
                ScanPoint("safe tool", (str(safe_cache),), "safe"),
                ScanPoint("confirm tool", (str(confirm_cache),), "confirm"),
            ]
            stdout = io.StringIO()

            with mock.patch.dict(DOMAINS, {"developer": points}), \
                    contextlib.redirect_stdout(stdout):
                status = main(
                    [
                        "clean",
                        "dev",
                        "--rules",
                        str(rules),
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(status, 0)
            self.assertEqual(payload["command"], "clean")
            self.assertEqual(payload["category"], "dev")
            self.assertEqual(payload["mode"], "preview")
            self.assertEqual(len(payload["categories"]), 1)
            category = payload["categories"][0]
            self.assertEqual(category["domain"], "developer")
            items = {item["category"]: item for item in category["items"]}
            self.assertTrue(items["safe tool"]["preselected"])
            self.assertFalse(items["confirm tool"]["preselected"])
            self.assertEqual(items["safe tool"]["domain"], "developer")

    def test_clean_without_category_scans_all_public_categories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rules = root / "rules.json"
            rules.write_text(
                json.dumps({"schema_version": 1}), encoding="utf-8"
            )
            replacements: dict[str, list[ScanPoint]] = {}
            for domain in ("system", "developer", "ai", "trash"):
                cache = root / domain
                cache.mkdir()
                (cache / "data.bin").write_bytes(domain.encode())
                replacements[domain] = [
                    ScanPoint(f"{domain} item", (str(cache),))
                ]
            stdout = io.StringIO()

            with mock.patch.dict(DOMAINS, replacements), \
                    contextlib.redirect_stdout(stdout):
                status = main(
                    ["clean", "--rules", str(rules), "--json"]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(status, 0)
            self.assertEqual(payload["category"], "all")
            self.assertEqual(
                {category["domain"] for category in payload["categories"]},
                {"system", "developer", "ai", "trash"},
            )

    def test_clean_all_assigns_overlapping_paths_to_specific_domain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / "Logs"
            jetbrains = logs / "JetBrains"
            jetbrains.mkdir(parents=True)
            general_log = logs / "general.log"
            idea_log = jetbrains / "idea.log"
            general_log.write_bytes(b"general")
            idea_log.write_bytes(b"jetbrains")
            rules = root / "rules.json"
            rules.write_text(
                json.dumps({"schema_version": 1}), encoding="utf-8"
            )
            replacements = {
                "system": [ScanPoint("用户日志", (str(logs),))],
                "developer": [
                    ScanPoint("JetBrains 日志", (str(jetbrains),))
                ],
                "ai": [],
                "trash": [],
            }
            stdout = io.StringIO()

            with mock.patch.dict(DOMAINS, replacements), \
                    contextlib.redirect_stdout(stdout):
                status = main(
                    ["clean", "--rules", str(rules), "--json"]
                )

            payload = json.loads(stdout.getvalue())
            categories = {
                category["domain"]: category
                for category in payload["categories"]
            }
            system_item = categories["system"]["items"][0]
            developer_item = categories["developer"]["items"][0]
            self.assertEqual(status, 0)
            general_allocated = general_log.stat().st_blocks * 512
            idea_allocated = idea_log.stat().st_blocks * 512
            self.assertEqual(
                payload["total_bytes"], general_allocated + idea_allocated
            )
            self.assertEqual(system_item["bytes"], general_allocated)
            self.assertEqual(system_item["logical_bytes"], len(b"general"))
            self.assertEqual(system_item["allocated_bytes"], general_allocated)
            self.assertFalse(system_item["preselected"])
            self.assertEqual(developer_item["bytes"], idea_allocated)
            self.assertEqual(
                developer_item["logical_bytes"], len(b"jetbrains")
            )
            self.assertEqual(
                developer_item["allocated_bytes"], idea_allocated
            )
            self.assertTrue(developer_item["preselected"])

    def test_clean_force_is_rejected_without_deleting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache"
            cache.mkdir()
            payload = cache / "data.bin"
            payload.write_bytes(b"content")
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                status = main(["clean", "dev", "--force"])

            self.assertEqual(status, 2)
            self.assertTrue(payload.exists())
            self.assertIn("拒绝执行删除", stderr.getvalue())

    def test_clean_force_json_error_is_machine_readable(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        with contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            status = main(["clean", "dev", "--force", "--json"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(status, 2)
        self.assertEqual(payload["command"], "clean")
        self.assertEqual(payload["status"], "error")
        self.assertEqual(
            payload["error"]["code"], "invalid_selection_options"
        )
        self.assertFalse(payload["executed"])
        self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
