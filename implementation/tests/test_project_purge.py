from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from openclean import __version__
from openclean.cleanup import CleanupOutcome, CleanupReport
from openclean.cli import _print_purge_report, main
from openclean.engine import (
    IgnoreRules,
    default_project_search_roots,
    scan_project_artifacts,
)
from openclean.knowledge_base import KnowledgeBase
from openclean.models import Item, ScanResult

SECONDS_PER_DAY = 24 * 60 * 60
TEST_SF_DATALESS = 0x40000000


def _with_dataless_flag(stat_result):
    return SimpleNamespace(
        st_mode=stat_result.st_mode,
        st_size=stat_result.st_size,
        st_blocks=stat_result.st_blocks,
        st_mtime=stat_result.st_mtime,
        st_dev=stat_result.st_dev,
        st_ino=stat_result.st_ino,
        st_nlink=stat_result.st_nlink,
        st_uid=stat_result.st_uid,
        st_flags=TEST_SF_DATALESS,
    )


def _write_artifact(path: Path, mtime: float) -> None:
    path.mkdir(parents=True)
    payload = path / "payload.bin"
    payload.write_bytes(b"artifact")
    os.utime(payload, (mtime, mtime))
    os.utime(path, (mtime, mtime))


class ProjectPurgeTests(unittest.TestCase):
    def test_vitepress_cleanup_preserves_configuration_and_theme_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            project = home / "project"
            vitepress = project / ".vitepress"
            theme = vitepress / "theme"
            theme.mkdir(parents=True)
            (project / "package.json").write_text("{}", encoding="utf-8")
            sources = [vitepress / "config.ts", theme / "index.ts"]
            for source in sources:
                source.write_text("export default {}\n", encoding="utf-8")
            old = time.time() - 9 * SECONDS_PER_DAY
            for name in ("cache", "dist", "dist-backup"):
                _write_artifact(vitepress / name, old)
            _write_artifact(theme / "dist", old)
            for path in [*sources, theme, vitepress]:
                os.utime(path, (old, old))
            rules = home / "rules.json"
            rules.write_text('{"schema_version": 1}', encoding="utf-8")
            (home / ".Trash").mkdir(mode=0o700)
            with mock.patch.dict(os.environ, {"HOME": str(home)}):
                result = scan_project_artifacts([project])
                self.assertEqual(
                    {item.path for item in result.items},
                    {vitepress / "cache", vitepress / "dist"},
                )
                with contextlib.redirect_stdout(io.StringIO()):
                    code = main(["purge", str(project), "--yes", "--json",
                                 "--rules", str(rules)])
            self.assertEqual(code, 0)
            self.assertTrue(all(source.exists() for source in sources))
            self.assertTrue((vitepress / "dist-backup/payload.bin").exists())
            self.assertTrue((theme / "dist/payload.bin").exists())
            self.assertFalse((vitepress / "cache").exists())
            self.assertFalse((vitepress / "dist").exists())
            self.assertTrue((home / ".Trash/cache/payload.bin").exists())
            self.assertTrue((home / ".Trash/dist/payload.bin").exists())

    def test_vitepress_explicit_unmarked_root_keeps_scoped_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            _write_artifact(project / ".vitepress/cache", time.time())
            result = scan_project_artifacts([project], include_unmarked_roots=True)
            self.assertEqual([item.artifact_name for item in result.items],
                             [".vitepress/cache"])

    def test_consequence_notes_preserve_selection_and_explain_artifact_age(self) -> None:
        from openclean.cli import _item_payload
        from openclean.scanpoints import project_artifact_note
        from openclean.tui import item_detail_lines

        now = 1_800_000_000.0
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            marker = project / "package.json"
            marker.write_text("{}", encoding="utf-8")
            os.utime(marker, (now, now))
            for name in ("node_modules", ".venv", "DerivedData", ".mypy_cache"):
                _write_artifact(project / name, now - 8 * SECONDS_PER_DAY)
            result = scan_project_artifacts([project], now=now)
            self.assertEqual(len(result.items), 4)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                _print_purge_report(result, False)
            for item in result.items:
                self.assertTrue(item.actionable)
                self.assertTrue(item.preselected)
                self.assertEqual(item.age_days, 8)
                self.assertIn("不代表整个项目的活跃度", item.note)
                self.assertIn(project_artifact_note(item.artifact_name), item.note)
                self.assertEqual(_item_payload(item)["note"], item.note)
                self.assertIn(item.note, "\n".join(item_detail_lines(item)))
                self.assertIn(item.note, stdout.getvalue())
            self.assertIn("依赖源", project_artifact_note("node_modules"))
            self.assertIn("不保证恢复", project_artifact_note(".venv"))
            self.assertIn("工具链", project_artifact_note("DerivedData"))
            self.assertIn("索引", project_artifact_note(".mypy_cache"))

    def test_text_result_describes_exact_selection_without_default_claims(
        self,
    ) -> None:
        project = Path("/Preview/Projects/demo")
        item = Item(
            path=project / "node_modules",
            size=8192,
            category="项目构建产物",
            safety="safe",
            domain="project",
            project_root=project,
            artifact_name="node_modules",
            age_days=1,
            preselected=True,
        )
        result = ScanResult(items=[item])
        report = CleanupReport(
            outcomes=[
                CleanupOutcome(
                    item=item,
                    status="moved_to_trash",
                    bytes_affected=item.size,
                    destination=Path("/Preview/.Trash/node_modules"),
                )
            ]
        )
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            _print_purge_report(result, False, report)

        output = stdout.getvalue()
        self.assertIn("项目清理执行结果", output)
        self.assertIn("当前选择", output)
        self.assertNotIn("只读预览", output)
        self.assertNotIn("默认预选", output)
        self.assertNotIn("超过 7 天", output)

    def test_dataless_project_search_root_is_not_enumerated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            search_root = Path(tmp) / "Projects"
            search_root.mkdir()
            root_stat = search_root.lstat()
            original_lstat = Path.lstat

            def fake_lstat(path: Path):
                stat_result = original_lstat(path)
                return (
                    _with_dataless_flag(root_stat)
                    if path == search_root
                    else stat_result
                )

            with mock.patch(
                "openclean.models.MACOS_SF_DATALESS", TEST_SF_DATALESS
            ), mock.patch.object(Path, "lstat", new=fake_lstat), mock.patch(
                "openclean.engine.os.scandir"
            ) as scandir:
                result = scan_project_artifacts([search_root])

            scandir.assert_not_called()
            self.assertEqual(result.items, [])
            self.assertEqual(
                result.issues[0].code, "dataless_directory_skipped"
            )

    def test_dataless_project_artifact_is_visible_but_not_measured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            artifact = project / "node_modules"
            artifact.mkdir(parents=True)
            (project / "package.json").write_text("{}", encoding="utf-8")
            artifact_stat = artifact.lstat()
            original_lstat = Path.lstat
            original_scandir = os.scandir

            def fake_lstat(path: Path):
                stat_result = original_lstat(path)
                return (
                    _with_dataless_flag(artifact_stat)
                    if path == artifact
                    else stat_result
                )

            def guarded_scandir(path):
                if Path(path) == artifact:
                    raise AssertionError("dataless artifact must not be enumerated")
                return original_scandir(path)

            with mock.patch(
                "openclean.models.MACOS_SF_DATALESS", TEST_SF_DATALESS
            ), mock.patch.object(Path, "lstat", new=fake_lstat), mock.patch(
                "openclean.engine.os.scandir", side_effect=guarded_scandir
            ):
                result = scan_project_artifacts([project])

            self.assertEqual(len(result.items), 1)
            item = result.items[0]
            self.assertEqual(item.path, artifact)
            self.assertEqual(item.size, 0)
            self.assertEqual(item.logical_size, 0)
            self.assertEqual(item.cloud_file_count, 1)
            self.assertFalse(item.actionable)
            self.assertEqual(item.safety, "critical")

    def test_cli_reports_openclean_version(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), self.assertRaises(SystemExit) as raised:
            main(["--version"])
        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(stdout.getvalue().strip(), f"openclean {__version__}")

    def test_default_search_roots_match_public_cli_contract(self) -> None:
        home = Path("/Users/example")
        self.assertEqual(
            default_project_search_roots(home),
            [
                home / "Projects",
                home / "Code",
                home / "dev",
                home / "GitHub",
                home / "Workspace",
            ],
        )

    def test_discovers_projects_groups_artifacts_and_applies_age_selection(self) -> None:
        now = 1_800_000_000.0
        old = now - 8 * SECONDS_PER_DAY
        recent = now - 2 * SECONDS_PER_DAY
        with tempfile.TemporaryDirectory() as tmp:
            search_root = Path(tmp) / "Projects"

            web = search_root / "web-app"
            web.mkdir(parents=True)
            (web / "package.json").write_text("{}", encoding="utf-8")
            _write_artifact(web / "node_modules", old)
            _write_artifact(web / "cmake-build-debug", old)

            nested = web / "native-module"
            nested.mkdir()
            (nested / "Cargo.toml").write_text("[package]", encoding="utf-8")
            _write_artifact(nested / "target", old)

            python_project = search_root / "python-service"
            python_project.mkdir(parents=True)
            (python_project / "pyproject.toml").write_text(
                "[project]", encoding="utf-8"
            )
            _write_artifact(python_project / ".venv", recent)

            decoy = search_root / "downloads"
            _write_artifact(decoy / "node_modules", old)

            result = scan_project_artifacts(
                [search_root], max_depth=6, now=now
            )

            by_path = {item.path: item for item in result.items}
            self.assertEqual(
                set(by_path),
                {
                    web / "node_modules",
                    web / "cmake-build-debug",
                    nested / "target",
                    python_project / ".venv",
                },
            )
            self.assertEqual(by_path[web / "node_modules"].project_root, web)
            self.assertEqual(by_path[nested / "target"].project_root, nested)
            self.assertTrue(by_path[web / "node_modules"].preselected)
            self.assertEqual(by_path[web / "node_modules"].age_days, 8)
            self.assertFalse(by_path[python_project / ".venv"].preselected)
            self.assertEqual(by_path[python_project / ".venv"].age_days, 2)
            self.assertNotIn(decoy / "node_modules", by_path)

    def test_detects_publicly_documented_artifact_names(self) -> None:
        now = 1_800_000_000.0
        old = now - 8 * SECONDS_PER_DAY
        expected = {
            "node_modules",
            ".next",
            ".turbo",
            "target",
            ".build",
            ".venv",
            "venv",
            "pycache",
            "Pods",
            "DerivedData",
            "vendor",
            "cmake-build-release",
        }
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            (project / ".git").mkdir()
            for name in expected:
                _write_artifact(project / name, old)

            result = scan_project_artifacts([project], now=now)

            self.assertEqual(
                {item.artifact_name for item in result.items}, expected
            )

    def test_old_artifact_with_protected_child_is_not_preselected(self) -> None:
        now = 1_800_000_000.0
        old = now - 8 * SECONDS_PER_DAY
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            (project / "package.json").write_text("{}", encoding="utf-8")
            artifact = project / "node_modules"
            _write_artifact(artifact, old)
            protected = artifact / "keep.bin"
            protected.write_bytes(b"protected-data")
            os.utime(protected, (old, old))
            os.utime(artifact, (old, old))
            knowledge_base = KnowledgeBase.from_mapping(
                {
                    "schema_version": 1,
                    "protect": {"paths": [str(protected)]},
                }
            )

            result = scan_project_artifacts(
                [project],
                ignore=IgnoreRules(knowledge_base=knowledge_base),
                now=now,
            )

            self.assertEqual(len(result.items), 1)
            self.assertFalse(result.items[0].preselected)
            self.assertEqual(result.items[0].excluded_paths, 1)
            self.assertEqual(
                result.items[0].size,
                (artifact / "payload.bin").stat().st_blocks * 512,
            )

    def test_cli_purge_emits_project_grouped_preview_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "unmarked-project"
            artifact = project / "node_modules"
            _write_artifact(artifact, time.time() - 8 * SECONDS_PER_DAY)
            rules = Path(tmp) / "rules.json"
            rules.write_text(
                json.dumps({"schema_version": 1}), encoding="utf-8"
            )
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                status = main(
                    [
                        "purge",
                        str(project),
                        "--rules",
                        str(rules),
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(status, 0)
            self.assertEqual(payload["command"], "purge")
            self.assertEqual(payload["mode"], "preview")
            self.assertEqual(len(payload["projects"]), 1)
            self.assertEqual(payload["projects"][0]["path"], str(project))
            self.assertEqual(
                payload["projects"][0]["artifacts"][0]["artifact_name"],
                "node_modules",
            )
            self.assertTrue(
                payload["projects"][0]["artifacts"][0]["preselected"]
            )

    def test_cli_force_is_rejected_without_deleting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            artifact = project / "node_modules"
            _write_artifact(artifact, time.time() - 8 * SECONDS_PER_DAY)
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                status = main(["purge", str(project), "--force"])

            self.assertEqual(status, 2)
            self.assertTrue(artifact.exists())
            self.assertIn("拒绝执行删除", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
