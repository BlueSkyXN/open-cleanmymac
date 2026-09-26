"""统一扫描界面：任务状态、暂停/继续、取消收尾与过期输入清理。"""
from __future__ import annotations

import tempfile
import signal
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from openclean import cli, engine, task_graph
from openclean.progress import ProgressTaskSpec, WeightedProgress
from openclean.scan_tui import (
    ScanJob,
    ScanScreenFailure,
    _run_scan_screen,
    start_scan_screen,
)
from openclean.scanpoints import ScanPoint
from openclean.tui import TUIUnavailable
from test_space_tui import _FakeScreen


class ScanJobTests(unittest.TestCase):
    def test_cli_signal_only_records_request_until_scanner_releases_locks(self):
        control = engine.Control()
        previous = signal.getsignal(signal.SIGINT)
        released = threading.Event()

        def scan():
            # 信号可能打断主线程正持有的非重入锁；handler 不能调用 cancel。
            with control._cancel._cond:
                signal.raise_signal(signal.SIGINT)
                signal.raise_signal(signal.SIGINT)
                self.assertTrue(control.interrupt_requested)
                self.assertFalse(control._cancel.is_set())
                with self.assertRaises(engine.Cancelled):
                    control.checkpoint()
            released.set()

        with self.assertRaises(KeyboardInterrupt):
            cli._run_interruptible_scan(control, scan)
        self.assertTrue(released.is_set() and control._cancel.is_set())
        self.assertIs(signal.getsignal(signal.SIGINT), previous)

    def test_cli_restores_signal_handler_on_success_and_error(self):
        previous = signal.getsignal(signal.SIGINT)
        self.assertEqual(cli._run_interruptible_scan(engine.Control(), lambda: 42), 42)
        self.assertIs(signal.getsignal(signal.SIGINT), previous)

        def fail():
            raise RuntimeError("fixture")
        with self.assertRaisesRegex(RuntimeError, "fixture"):
            cli._run_interruptible_scan(engine.Control(), fail)
        self.assertIs(signal.getsignal(signal.SIGINT), previous)

    def test_cli_preserves_embedded_callers_custom_signal_handler(self):
        with mock.patch.object(cli.signal, "getsignal", return_value=lambda *args: None), \
                mock.patch.object(cli.signal, "signal") as replace_handler:
            self.assertEqual(cli._run_interruptible_scan(engine.Control(), lambda: 42), 42)
        replace_handler.assert_not_called()

    def test_interrupt_while_submitting_tasks_also_cancels_running_workers(self):
        control = engine.Control()
        started = threading.Event()
        executor = engine.ThreadPoolExecutor

        class InterruptedExecutor(executor):
            calls = 0

            def submit(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 2:
                    if not started.wait(2):
                        raise AssertionError("first worker 未启动")
                    raise KeyboardInterrupt
                return super().submit(*args, **kwargs)

        def scan(*args, **kwargs):
            started.set()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                control.checkpoint()
                time.sleep(0.001)
            return engine.ScanResult()

        with mock.patch.object(engine, "ThreadPoolExecutor", InterruptedExecutor), \
                mock.patch.object(engine, "_scan_point", side_effect=scan), \
                self.assertRaises(KeyboardInterrupt):
            engine.scan_points([ScanPoint("a", ()), ScanPoint("b", ())], ctl=control, workers=2)
        self.assertTrue(control._cancel.is_set())

    def test_sigint_in_json_mode_drains_workers_without_reading_stdin(self):
        from scripts.benchmark_scan_ui import probe_json_cancel
        for command in ("analyze", "clean", "purge"):
            with self.subTest(command=command):
                self.assertTrue(probe_json_cancel(command)["json_valid"])

    def test_sigint_delivered_to_worker_wakes_coordinator(self):
        from scripts.benchmark_scan_ui import probe_json_cancel
        for command in ("analyze", "clean", "purge"):
            with self.subTest(command=command):
                self.assertTrue(probe_json_cancel(command, signal_from_worker=True)["json_valid"])

    def test_all_parallel_workers_pause_and_resume_together(self):
        gates = [threading.Event(), threading.Event()]
        release = threading.Event()

        def scan(point, control, *args, **kwargs):
            gates[int(point.category)].set()
            while not release.is_set():
                control.checkpoint()
                time.sleep(0.001)
            return engine.ScanResult()

        def action(control, publish):
            return engine.scan_points([ScanPoint(str(i), ()) for i in range(2)],
                                      ctl=control, workers=2, on_progress=publish)

        with mock.patch.object(engine, "_scan_point", side_effect=scan):
            job = ScanJob(action)
            try:
                job.start()
                self.assertTrue(all(gate.wait(2) for gate in gates))
                for _ in range(3):
                    job.request_pause()
                    self.assertTrue(job.control.pause_confirmed.wait(2))
                    self.assertTrue(job.paused)
                    job.resume()
                    self.assertFalse(job.paused)
            finally:
                release.set()
                job.close()

    def test_graph_interrupt_signals_cancel_before_waiting_for_workers(self):
        started, cancelled, finished = threading.Event(), threading.Event(), threading.Event()

        def worker():
            started.set()
            if cancelled.wait(2):
                finished.set()

        def interrupt(*args, **kwargs):
            self.assertTrue(started.wait(2))
            raise KeyboardInterrupt

        with mock.patch.object(task_graph, "wait", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            task_graph.execute_task_graph([task_graph.TaskSpec("fixture", worker)], workers=1,
                                          on_abort=cancelled.set)
        self.assertTrue(finished.is_set())

    def test_one_paused_worker_does_not_hide_another_workers_blocked_io(self):
        waiting = threading.Event()
        blocked = threading.Event()
        release = threading.Event()

        def scan(point, control, *args, **kwargs):
            if point.category == "blocked":
                blocked.set()
                release.wait(3)
            else:
                waiting.set()
                while not release.is_set():
                    control.checkpoint()
                    time.sleep(0.001)
            return engine.ScanResult()

        def action(control, publish):
            return engine.scan_points([ScanPoint("waiting", ()), ScanPoint("blocked", ())],
                                      ctl=control, workers=2, on_progress=publish)

        with mock.patch.object(engine, "_scan_point", side_effect=scan):
            job = ScanJob(action)
            try:
                job.start()
                self.assertTrue(waiting.wait(2) and blocked.wait(2))
                job.request_pause()
                time.sleep(0.05)
                self.assertFalse(job.paused, "另一个 worker 仍在 I/O，不能声称全部已暂停")
            finally:
                release.set()
                job.close()

    def test_pause_confirmed_only_when_worker_reaches_checkpoint(self) -> None:
        gate = threading.Event()
        released = threading.Event()

        def no_checkpoint(control, publish):
            gate.set()
            released.wait(2)
            return "result"

        job = ScanJob(no_checkpoint)
        try:
            job.start()
            self.assertTrue(gate.wait(2))
            job.request_pause()
            self.assertFalse(job.paused)
        finally:
            released.set()
            job.close()

        checkpointed = threading.Event()

        def looping(control, publish):
            checkpointed.set()
            try:
                while True:
                    control.checkpoint()
                    time.sleep(0.001)
            except engine.Cancelled:
                return "cancelled"

        job = ScanJob(looping)
        try:
            job.start()
            self.assertTrue(checkpointed.wait(2))
            job.request_pause()
            deadline = time.monotonic() + 2
            while not job.paused and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(job.paused)
            job.resume()
            self.assertFalse(job.paused)
        finally:
            job.close()

    def test_close_drains_worker_and_is_idempotent(self) -> None:
        stopped = threading.Event()

        def looping(control, publish):
            try:
                while True:
                    control.checkpoint()
                    time.sleep(0.001)
            except engine.Cancelled:
                stopped.set()

        job = ScanJob(looping)
        job.start()
        time.sleep(0.05)
        job.close()
        job.close()
        self.assertTrue(stopped.wait(2))
        self.assertFalse(
            any(t.name.startswith("cleanup-scan") for t in threading.enumerate())
        )


class ScanScreenTests(unittest.TestCase):
    def test_completion_returns_result_and_drains_stale_space(self) -> None:
        completed = threading.Event()

        def fast(control, publish):
            publish(_static_snapshot())
            completed.set()
            return "scan-result"

        class Screen(_FakeScreen):
            def getch(self):
                if not completed.wait(2):
                    raise AssertionError("worker 未启动")
                if self.keys:
                    return self.keys.pop(0)
                return -1

        screen = Screen([ord(" ")])
        outcome = _run_scan_screen(
            screen, fast, title="Clean · 扫描", scope="fixture"
        )
        self.assertEqual(outcome.status, "done")
        self.assertEqual(outcome.result, "scan-result")
        self.assertIsNone(outcome.error)
        self.assertEqual(screen.keys, [])
        self.assertTrue(any("正在扫描" in line for line in screen.lines))

    def test_q_cancels_and_drains_worker(self) -> None:
        started = threading.Event()
        stopped = threading.Event()

        def looping(control, publish):
            started.set()
            try:
                while True:
                    control.checkpoint()
                    time.sleep(0.001)
            except engine.Cancelled:
                stopped.set()

        screen = _FakeScreen([ord("q")])

        def getch():
            self.assertTrue(started.wait(2))
            return ord("q")

        screen.getch = getch
        outcome = _run_scan_screen(
            screen, looping, title="Clean · 扫描", scope="fixture"
        )
        self.assertEqual(outcome.status, "cancelled")
        self.assertTrue(stopped.wait(2))
        self.assertFalse(
            any(t.name.startswith("cleanup-scan") for t in threading.enumerate())
        )
        self.assertTrue(any("正在停止" in line for line in screen.lines))

    def test_pause_resume_states_and_completion_priority(self) -> None:
        started = threading.Event()
        finish = threading.Event()

        def slow(control, publish):
            started.set()
            while not finish.is_set():
                control.checkpoint()
                time.sleep(0.001)
            return "scan-result"

        class Screen(_FakeScreen):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.pause_sent = False

            def getch(self):
                if not started.wait(2):
                    raise AssertionError("worker 未启动")
                if not self.pause_sent and any("正在扫描" in line for line in self.lines):
                    self.pause_sent = True
                    return ord(" ")
                if not finish.is_set() and any("已暂停" in line for line in self.lines):
                    finish.set()
                    return ord(" ")
                time.sleep(0.01)
                return -1

        outcome = _run_scan_screen(
            Screen([]), slow, title="Purge · 扫描", scope="fixture"
        )
        self.assertEqual(outcome.status, "done")
        self.assertEqual(outcome.result, "scan-result")

    def test_q_during_pause_request_still_cancels_and_drains(self) -> None:
        gate = threading.Event()
        released = threading.Event()

        def blocked_io(control, publish):
            # 模拟阻塞 I/O：暂停已请求但 worker 尚未到达检查点。
            gate.set()
            released.wait(3)
            return "unused"

        class Screen(_FakeScreen):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.pause_sent = False

            def getch(self):
                if not gate.wait(2):
                    raise AssertionError("worker 未启动")
                if not self.pause_sent and any("正在扫描" in line for line in self.lines):
                    self.pause_sent = True
                    return ord(" ")
                if any("暂停已请求" in line for line in self.lines):
                    return ord("q")
                time.sleep(0.01)
                return -1

        screen = Screen([])
        outcome = _run_scan_screen(
            screen, blocked_io, title="Clean · 扫描", scope="fixture"
        )
        self.assertEqual(outcome.status, "cancelled")
        released.set()
        self.assertTrue(any("暂停已请求" in line for line in screen.lines))
        self.assertTrue(any("正在停止" in line for line in screen.lines))
        self.assertFalse(
            any(t.name.startswith("cleanup-scan") for t in threading.enumerate())
        )

    def test_mid_scan_draw_failure_cancels_worker(self) -> None:
        started = threading.Event()
        stopped = threading.Event()

        def looping(control, publish):
            started.set()
            try:
                while True:
                    control.checkpoint()
                    time.sleep(0.001)
            except engine.Cancelled:
                stopped.set()

        import curses

        class Screen(_FakeScreen):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.frames = 0

            def erase(self):
                self.frames += 1
                if self.frames > 2:
                    raise curses.error("fixture terminal failure")
                super().erase()

        with self.assertRaises(ScanScreenFailure):
            _run_scan_screen(Screen([]), looping, title="Clean · 扫描", scope="fixture")
        self.assertTrue(stopped.wait(2))
        self.assertFalse(
            any(t.name.startswith("cleanup-scan") for t in threading.enumerate())
        )

    def test_init_failure_becomes_tui_unavailable(self) -> None:
        import curses

        with mock.patch("curses.wrapper", side_effect=curses.error("no terminal")), \
                self.assertRaises(TUIUnavailable):
            start_scan_screen(
                lambda control, publish: "unused",
                title="Clean · 扫描",
                scope="fixture",
            )


def _static_snapshot():
    from openclean.progress import WeightedProgress

    progress = WeightedProgress(
        [ProgressTaskSpec("a", "任务 A", 1), ProgressTaskSpec("b", "任务 B", 1)]
    )
    progress.task("a").start()
    return progress.snapshot()


class TaskStartedTests(unittest.TestCase):
    def test_dynamic_scanner_reports_started_before_external_query(self):
        progress = WeightedProgress([ProgressTaskSpec("docker", "Docker")])
        protection = engine.IgnoreRules()

        def scan():
            self.assertTrue(progress.snapshot().tasks[0].started)
            return engine.ScanResult()

        with mock.patch.object(engine, "scan_docker_resources", side_effect=scan):
            engine._scan_dynamic_point_with_progress(
                ScanPoint("Docker", (), scanner="docker"), engine.Control(), protection,
                progress.task("docker"), engine._scan_guard_context(protection))

    def test_started_marks_real_running_task(self) -> None:
        progress = WeightedProgress(
            [ProgressTaskSpec("a", "任务 A", 1), ProgressTaskSpec("b", "任务 B", 1)]
        )
        progress.start()
        self.assertEqual(progress.snapshot().active_label, "准备中")
        progress.task("a").start()
        snapshot = progress.snapshot()
        self.assertTrue(snapshot.tasks[0].started)
        self.assertFalse(snapshot.tasks[1].started)
        self.assertEqual(snapshot.active_label, "任务 A")
        progress.task("a").complete()
        final = progress.snapshot()
        self.assertTrue(final.tasks[0].complete)
        self.assertEqual(final.active_label, "准备中")

    def test_scan_points_report_started_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "cache").mkdir()
            snapshots = []
            engine.scan_points(
                [ScanPoint("test", (str(root / "cache"),))],
                workers=1,
                on_progress=snapshots.append,
            )
        self.assertTrue(snapshots)
        self.assertTrue(any(snapshot.tasks[0].started for snapshot in snapshots))
        self.assertTrue(snapshots[-1].tasks[0].complete)


if __name__ == "__main__":
    unittest.main()
