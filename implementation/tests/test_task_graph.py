from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from openclean.engine import Cancelled, scan_domains
from openclean.models import ScanResult
from openclean.scanpoints import DOMAINS, ScanPoint
from openclean.task_graph import (
    TaskGraphError,
    TaskSpec,
    execute_task_graph,
)


class TaskGraphValidationTests(unittest.TestCase):
    def test_rejects_duplicate_unknown_self_and_cyclic_dependencies(self) -> None:
        def action() -> None:
            return None

        with self.assertRaisesRegex(TaskGraphError, "唯一"):
            execute_task_graph(
                [TaskSpec("same", action), TaskSpec("same", action)],
                workers=1,
            )
        with self.assertRaisesRegex(TaskGraphError, "未知依赖"):
            execute_task_graph(
                [TaskSpec("task", action, dependencies=("missing",))],
                workers=1,
            )
        with self.assertRaisesRegex(TaskGraphError, "依赖自身"):
            TaskSpec("task", action, dependencies=("task",))
        with self.assertRaisesRegex(TaskGraphError, "存在环"):
            execute_task_graph(
                [
                    TaskSpec("first", action, dependencies=("second",)),
                    TaskSpec("second", action, dependencies=("first",)),
                ],
                workers=2,
            )

    def test_validation_happens_before_any_action(self) -> None:
        calls = []

        with self.assertRaises(TaskGraphError):
            execute_task_graph(
                [
                    TaskSpec("valid", lambda: calls.append("ran")),
                    TaskSpec("invalid", lambda: None, dependencies=("missing",)),
                ],
                workers=1,
            )

        self.assertEqual(calls, [])

    def test_rejects_invalid_worker_count(self) -> None:
        with self.assertRaisesRegex(TaskGraphError, "workers"):
            execute_task_graph([], workers=0)


class TaskGraphExecutionTests(unittest.TestCase):
    def test_dependencies_run_after_successful_prerequisites(self) -> None:
        calls = []

        result = execute_task_graph(
            [
                TaskSpec("prepare", lambda: calls.append("prepare") or 1),
                TaskSpec(
                    "finish",
                    lambda: calls.append("finish") or 2,
                    dependencies=("prepare",),
                ),
            ],
            workers=2,
        )

        self.assertEqual(calls, ["prepare", "finish"])
        self.assertEqual(
            [outcome.value for outcome in result.outcomes],
            [1, 2],
        )

    def test_independent_tasks_really_run_concurrently(self) -> None:
        barrier = threading.Barrier(2, timeout=2)

        def rendezvous(name: str) -> str:
            barrier.wait()
            return name

        result = execute_task_graph(
            [
                TaskSpec("first", lambda: rendezvous("first")),
                TaskSpec("second", lambda: rendezvous("second")),
            ],
            workers=2,
        )

        self.assertEqual(
            [outcome.value for outcome in result.outcomes],
            ["first", "second"],
        )

    def test_failure_blocks_only_dependents_and_preserves_input_order(self) -> None:
        calls = []

        def fail():
            calls.append("failed")
            raise RuntimeError("boom")

        result = execute_task_graph(
            [
                TaskSpec("failed", fail),
                TaskSpec(
                    "blocked",
                    lambda: calls.append("blocked"),
                    dependencies=("failed",),
                ),
                TaskSpec("independent", lambda: calls.append("independent") or 3),
            ],
            workers=2,
        )

        failed, blocked, independent = result.outcomes
        self.assertIsInstance(failed.error, RuntimeError)
        self.assertEqual(blocked.blocked_by, ("failed",))
        self.assertFalse(blocked.executed)
        self.assertEqual(independent.value, 3)
        self.assertNotIn("blocked", calls)
        self.assertEqual(
            [outcome.identifier for outcome in result.outcomes],
            ["failed", "blocked", "independent"],
        )

    def test_transitive_blocking_and_readonly_lookup(self) -> None:
        result = execute_task_graph(
            [
                TaskSpec("root", lambda: (_ for _ in ()).throw(ValueError("bad"))),
                TaskSpec("middle", lambda: None, dependencies=("root",)),
                TaskSpec("leaf", lambda: None, dependencies=("middle",)),
            ],
            workers=1,
        )

        self.assertEqual(result.by_identifier["middle"].blocked_by, ("root",))
        self.assertEqual(result.by_identifier["leaf"].blocked_by, ("middle",))
        with self.assertRaises(TypeError):
            result.by_identifier["new"] = result.outcomes[0]


class ScanDomainGraphIntegrationTests(unittest.TestCase):
    def test_filesystem_and_dynamic_scanners_run_concurrently(self) -> None:
        barrier = threading.Barrier(2, timeout=2)
        points = [
            ScanPoint("Filesystem", ()),
            ScanPoint("Docker", (), scanner="docker"),
        ]

        def filesystem(*_):
            barrier.wait()
            return ScanResult()

        def docker():
            barrier.wait()
            return ScanResult()

        with mock.patch.dict(DOMAINS, {"developer": points}), mock.patch(
            "openclean.engine._scan_point",
            side_effect=filesystem,
        ), mock.patch(
            "openclean.engine.scan_docker_resources",
            side_effect=docker,
        ):
            result = scan_domains(["developer"], workers=2)

        self.assertTrue(result.complete)

    def test_dynamic_failure_does_not_discard_filesystem_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache"
            cache.mkdir()
            (cache / "data.bin").write_bytes(b"data")
            points = [
                ScanPoint("Filesystem", (str(cache),)),
                ScanPoint("Docker", (), scanner="docker"),
            ]

            with mock.patch.dict(DOMAINS, {"developer": points}), mock.patch(
                "openclean.engine.scan_docker_resources",
                side_effect=RuntimeError("daemon failed"),
            ):
                result = scan_domains(["developer"], workers=2)

            self.assertFalse(result.complete)
            self.assertEqual([item.category for item in result.items], ["Filesystem"])
            self.assertEqual(result.issues[0].code, "task_failed")
            self.assertEqual(result.issues[0].task, "Docker")

    def test_cancelled_task_propagates_to_domain_result(self) -> None:
        points = [ScanPoint("First", ()), ScanPoint("Second", ())]

        with mock.patch.dict(DOMAINS, {"system": points}), mock.patch(
            "openclean.engine._scan_point",
            side_effect=Cancelled,
        ):
            result = scan_domains(["system"], workers=2)

        self.assertTrue(result.cancelled)
        self.assertFalse(result.complete)


if __name__ == "__main__":
    unittest.main()
