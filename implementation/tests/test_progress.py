from __future__ import annotations

import contextlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from openclean.cli import main
from openclean.engine import scan_domains, scan_points, scan_project_artifacts
from openclean.models import ScanResult
from openclean.progress import (
    ProgressTaskSpec,
    TerminalProgressRenderer,
    WeightedProgress,
)
from openclean.scanpoints import DOMAINS, ScanPoint


class _TTYBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


class WeightedProgressTests(unittest.TestCase):
    def test_weighted_average_formula_and_immutable_snapshot(self) -> None:
        progress = WeightedProgress(
            (
                ProgressTaskSpec("small", "Small", 1),
                ProgressTaskSpec("large", "Large", 3),
            )
        )
        progress.task("small").set_fraction(0.5)
        progress.task("large").set_fraction(0.25)

        snapshot = progress.snapshot()

        self.assertAlmostEqual(snapshot.fraction, 0.3125)
        self.assertEqual(snapshot.total_tasks, 2)
        self.assertEqual(snapshot.completed_tasks, 0)
        self.assertEqual(snapshot.tasks[0].fraction, 0.5)
        with self.assertRaises((AttributeError, TypeError)):
            snapshot.fraction = 1.0  # type: ignore[misc]

    def test_updates_are_monotonic_and_complete_reaches_one(self) -> None:
        snapshots = []
        progress = WeightedProgress(
            (ProgressTaskSpec("task", "Task"),),
            callback=snapshots.append,
            smoothing_items=4,
        )
        progress.start()
        task = progress.task("task")
        task.advance()
        task.set_fraction(0.8)
        task.set_fraction(0.2)
        task.complete()

        fractions = [snapshot.fraction for snapshot in snapshots]
        self.assertEqual(fractions, sorted(fractions))
        self.assertEqual(fractions[-1], 1.0)
        self.assertEqual(snapshots[-1].completed_tasks, 1)
        self.assertEqual(snapshots[-1].processed_items, 1)

    def test_concurrent_updates_preserve_sequence_and_counts(self) -> None:
        snapshots = []
        progress = WeightedProgress(
            tuple(
                ProgressTaskSpec(f"task-{index}", f"Task {index}")
                for index in range(4)
            ),
            callback=snapshots.append,
        )
        progress.start()

        def run(identifier: str) -> None:
            task = progress.task(identifier)
            for _ in range(100):
                task.advance()
            task.complete()

        threads = [
            threading.Thread(target=run, args=(f"task-{index}",))
            for index in range(4)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        final = progress.snapshot()
        self.assertEqual(final.processed_items, 400)
        self.assertEqual(final.completed_tasks, 4)
        self.assertEqual(final.fraction, 1.0)
        self.assertEqual(
            [snapshot.sequence for snapshot in snapshots],
            sorted(snapshot.sequence for snapshot in snapshots),
        )
        self.assertEqual(
            [snapshot.fraction for snapshot in snapshots],
            sorted(snapshot.fraction for snapshot in snapshots),
        )

    def test_observer_failure_never_breaks_progress_owner(self) -> None:
        progress = WeightedProgress(
            (ProgressTaskSpec("task", "Task"),),
            callback=lambda _: (_ for _ in ()).throw(RuntimeError("ui failed")),
        )

        progress.start()
        progress.task("task").complete()

        self.assertEqual(progress.snapshot().fraction, 1.0)

    def test_terminal_renderer_is_single_line_and_finish_is_idempotent(self) -> None:
        stream = io.StringIO()
        renderer = TerminalProgressRenderer(
            stream=stream,
            min_interval=0,
            bar_width=10,
        )
        progress = WeightedProgress(
            (ProgressTaskSpec("task", "Scanning"),),
            callback=renderer,
        )
        progress.start()
        progress.task("task").set_fraction(0.5)
        progress.task("task").complete()
        renderer.finish()
        renderer.finish()

        output = stream.getvalue()
        self.assertIn("50%", output)
        self.assertIn("100%", output)
        self.assertEqual(output.count("\n"), 1)


class EngineProgressTests(unittest.TestCase):
    def test_scan_points_reports_fixed_task_count_and_final_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            (first / "a.bin").write_bytes(b"a")
            (second / "b.bin").write_bytes(b"b")
            snapshots = []

            result = scan_points(
                [
                    ScanPoint("First", (str(first),)),
                    ScanPoint("Second", (str(second),)),
                ],
                workers=2,
                on_progress=snapshots.append,
            )

            self.assertTrue(result.complete)
            self.assertEqual(snapshots[0].total_tasks, 2)
            self.assertEqual(snapshots[-1].completed_tasks, 2)
            self.assertEqual(snapshots[-1].fraction, 1.0)
            self.assertGreaterEqual(snapshots[-1].processed_items, 4)
            self.assertEqual(
                [snapshot.fraction for snapshot in snapshots],
                sorted(snapshot.fraction for snapshot in snapshots),
            )

    def test_project_scan_reports_discovery_and_artifact_phases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            artifact = project / "node_modules"
            artifact.mkdir(parents=True)
            (project / "package.json").write_text("{}", encoding="utf-8")
            (artifact / "data.bin").write_bytes(b"data")
            snapshots = []

            result = scan_project_artifacts(
                [project], on_progress=snapshots.append
            )

            self.assertTrue(result.complete)
            self.assertEqual(snapshots[0].total_tasks, 2)
            self.assertEqual(
                {task.identifier for task in snapshots[-1].tasks},
                {"project-discovery", "project-artifacts"},
            )
            self.assertEqual(snapshots[-1].fraction, 1.0)

    def test_domain_progress_includes_dynamic_scanners_from_start(self) -> None:
        points = [
            ScanPoint("Filesystem", ()),
            ScanPoint("Docker", (), scanner="docker"),
        ]
        snapshots = []

        with mock.patch.dict(DOMAINS, {"developer": points}), mock.patch(
            "openclean.engine.scan_docker_resources",
            return_value=ScanResult(),
        ):
            result = scan_domains(
                ["developer"], workers=1, on_progress=snapshots.append
            )

        self.assertTrue(result.complete)
        self.assertEqual(snapshots[0].total_tasks, 2)
        self.assertEqual(snapshots[-1].completed_tasks, 2)
        self.assertEqual(snapshots[-1].fraction, 1.0)

    def test_cli_progress_is_tty_only_and_never_pollutes_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "cache"
            cache.mkdir()
            (cache / "data.bin").write_bytes(b"data")
            rules = root / "rules.json"
            rules.write_text(
                json.dumps({"schema_version": 1}), encoding="utf-8"
            )
            points = [ScanPoint("Test", (str(cache),))]

            text_stdout = io.StringIO()
            text_stderr = _TTYBuffer()
            with mock.patch.dict(DOMAINS, {"developer": points}), \
                    contextlib.redirect_stdout(text_stdout), \
                    contextlib.redirect_stderr(text_stderr):
                text_status = main(
                    [
                        "clean",
                        "dev",
                        "--no-interactive",
                        "--rules",
                        str(rules),
                    ]
                )

            json_stdout = io.StringIO()
            json_stderr = _TTYBuffer()
            with mock.patch.dict(DOMAINS, {"developer": points}), \
                    contextlib.redirect_stdout(json_stdout), \
                    contextlib.redirect_stderr(json_stderr):
                json_status = main(
                    [
                        "clean",
                        "dev",
                        "--rules",
                        str(rules),
                        "--json",
                    ]
                )

            self.assertEqual(text_status, 0)
            self.assertIn("扫描 [", text_stderr.getvalue())
            self.assertEqual(json_status, 0)
            self.assertEqual(json_stderr.getvalue(), "")
            self.assertEqual(json.loads(json_stdout.getvalue())["mode"], "preview")


if __name__ == "__main__":
    unittest.main()
