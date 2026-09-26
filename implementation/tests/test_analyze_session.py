from __future__ import annotations

import contextlib
import curses
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from openclean import analyzer as analyzer_module, engine, space_tui
from openclean.analyzer import AnalyzeError, SpaceAnalysis, SpaceEntry, analyze_path
from openclean.cli import main
from openclean.models import Item, ScanIssue
from openclean.processes import ProcessDetectionError
from openclean.scanpoints import ScanPoint
from openclean.space_session import AnalysisJob, SpaceCache
from openclean.space_tui import SpaceReviewResult, _run_space_review
from test_space_tui import _FakeScreen


class AnalyzeSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.root = self.home / "data"
        self.root.mkdir()
        env = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)

    def review(self, screen, analyzer=analyze_path):
        with mock.patch("curses.curs_set"):
            return _run_space_review(screen, self.root, protection=engine.IgnoreRules(), top=0,
                                     allow_execution=False, analyzer=analyzer)

    def test_bulk_selection_preserves_other_branches_and_replaces_only_descendants(self):
        parents = [Item(self.root / name, 100, "fixture") for name in ("a", "b")]
        child = Item(self.root / "a/child", 10, "fixture")
        outside = Item(self.home / "outside", 20, "fixture")
        selected = {item.path: item for item in (child, outside)}
        index = space_tui.SelectionIndex(selected)
        index.rebuild()
        space_tui._select_level(parents, selected, index)
        self.assertEqual(set(selected), {item.path for item in [*parents, outside]})
        index.rebuild()
        space_tui._select_level([child], selected, index)
        self.assertNotIn(child.path, selected)

    def test_processing_path_follows_real_nested_directory(self):
        nested = self.root / "outer/nested"
        nested.mkdir(parents=True)
        (nested / "file").write_bytes(b"fixture")
        released = threading.Event()
        seen = []
        scandir = engine.scandir_entries

        def blocked(path):
            if path == nested:
                if not released.wait(3):
                    raise AssertionError("没有发布实际嵌套路径")
            return scandir(path)

        def update(snapshot):
            if str(nested) in snapshot.current_paths:
                seen.append(snapshot)
                released.set()

        with mock.patch.object(engine, "scandir_entries", side_effect=blocked):
            analysis = analyze_path(self.root, on_update=update)
        self.assertTrue(seen)
        self.assertTrue(analysis.complete)

    def test_first_frame_precedes_slow_worker_and_q_cancels_at_checkpoint(self):
        started, finished = threading.Event(), threading.Event()
        owner = threading.get_ident()
        test = self

        class Screen(_FakeScreen):
            def refresh(self):
                test.assertEqual(threading.get_ident(), owner)

            def getch(self):
                test.assertTrue(started.wait(2))
                test.assertTrue(any("正在扫描" in line for line in self.lines))
                test.assertFalse(finished.is_set())
                return ord("q")

        screen = Screen([])

        def slow(path, *, control, **_):
            self.assertTrue(any("正在扫描" in line for line in screen.lines))
            started.set()
            try:
                while True:
                    control.checkpoint()
                    time.sleep(0.001)
            except engine.Cancelled:
                return SpaceAnalysis(path, cancelled=True)
            finally:
                finished.set()

        result = self.review(screen, slow)
        self.assertTrue(result.cancelled)
        self.assertTrue(finished.is_set())
        self.assertFalse(any(t.name.startswith("analyze-session") for t in threading.enumerate()))

    def test_keyboard_interrupt_drains_worker_before_propagating(self):
        started, stopped = threading.Event(), threading.Event()

        def slow(path, *, control, **_):
            started.set()
            try:
                while True:
                    control.checkpoint()
                    time.sleep(0.001)
            except engine.Cancelled:
                return SpaceAnalysis(path, cancelled=True)
            finally:
                stopped.set()

        class Screen(_FakeScreen):
            def getch(self):
                if not started.wait(2):
                    raise AssertionError("worker 未启动")
                raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.review(Screen([]), slow)
        self.assertTrue(stopped.is_set())

    def test_stopping_frame_failure_still_drains_worker_and_restores_input(self):
        for error, interrupted in ((error, interrupted)
                                   for error in (curses.error, OSError, KeyboardInterrupt)
                                   for interrupted in (False, True)):
            with self.subTest(error=error.__name__, interrupted=interrupted):
                started, stopped = threading.Event(), threading.Event()
                jobs = []

                def slow(path, *, control, **_):
                    started.set()
                    try:
                        while True:
                            control.checkpoint()
                            time.sleep(0.001)
                    except engine.Cancelled:
                        return SpaceAnalysis(path, cancelled=True)
                    finally:
                        stopped.set()

                class Screen(_FakeScreen):
                    def getch(self):
                        if not started.wait(2):
                            raise AssertionError("worker 未启动")
                        if interrupted:
                            raise KeyboardInterrupt
                        return ord("q")

                def create_job(*args):
                    job = AnalysisJob(*args)
                    jobs.append(job)
                    return job

                draw = space_tui._draw_loading

                def fail_stopping(*args, **kwargs):
                    if kwargs.get("stopping"):
                        raise error("fixture terminal failure")
                    return draw(*args, **kwargs)

                screen = Screen([])
                try:
                    expected = (self.assertRaises(KeyboardInterrupt) if interrupted or error is KeyboardInterrupt
                                else contextlib.nullcontext())
                    with mock.patch.object(space_tui, "AnalysisJob", side_effect=create_job), \
                            mock.patch.object(space_tui, "_draw_loading", side_effect=fail_stopping), \
                            expected:
                        self.assertTrue(self.review(screen, slow).cancelled)
                    self.assertTrue(stopped.is_set(), "绘制异常绕过了后台线程收尾")
                    self.assertEqual(screen.delay, -1)
                finally:
                    # 旧实现复现失败时也保证测试自己不遗留线程。
                    for job in jobs:
                        job.close()

    def test_sigint_during_close_waits_for_actual_worker_completion(self):
        # 信号只发给隔离子进程，避免干扰 unittest 自身的线程和信号状态。
        child = '''
import json, os, signal, sys, threading
from pathlib import Path
from openclean.analyzer import SpaceAnalysis
from openclean.engine import IgnoreRules
from openclean.space_session import AnalysisJob
started, release, finished = threading.Event(), threading.Event(), threading.Event()
def slow(path, **kwargs):
    started.set()
    release.wait(3)
    finished.set()
    return SpaceAnalysis(path)
job = AnalysisJob(Path(sys.argv[1]), IgnoreRules(), slow)
job.start()
assert started.wait(2)
interrupt = threading.Timer(0.05, lambda: os.kill(os.getpid(), signal.SIGINT))
unblock = threading.Timer(0.2, release.set)
interrupt.start()
unblock.start()
try:
    job.close()
    print(json.dumps({"finished": finished.is_set(), "done": job.done.is_set()}))
finally:
    release.set()
    assert finished.wait(2)
    interrupt.join()
    unblock.join()
    job.close()
'''
        result = subprocess.run([sys.executable, "-c", child, str(self.root)],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"finished": True, "done": True})

    def test_close_before_start_and_after_completion(self):
        job = AnalysisJob(self.root, engine.IgnoreRules())
        job.close()
        job.close()
        job = AnalysisJob(self.root, engine.IgnoreRules())
        try:
            job.start()
            self.assertTrue(job.done.wait(2))
        finally:
            job.close()
        job.close()

    def test_browser_keys_do_not_repeat_every_issue_path_comparison(self):
        for index in range(12):
            (self.root / f"file-{index}").write_bytes(b"data")
        analysis = analyze_path(self.root)
        analysis.issues = [ScanIssue("permission_denied", "fixture denied", "analyze",
                                     self.root / "other" / str(index)) for index in range(500)]
        with mock.patch.object(space_tui, "_same_or_descendant",
                               wraps=space_tui._same_or_descendant) as compare:
            self.review(_FakeScreen([curses.KEY_DOWN, curses.KEY_UP, ord("q")]),
                        mock.Mock(return_value=analysis))
        self.assertLess(compare.call_count, len(analysis.issues))

    def test_issue_scrolling_wraps_only_the_viewed_lines(self):
        analysis = SpaceAnalysis(self.root, issues=[
            ScanIssue("permission_denied", f"fixture denied {index}", "analyze", self.root)
            for index in range(500)
        ])
        screen = _FakeScreen([ord("e"), curses.KEY_DOWN, curses.KEY_UP, 27, ord("q")])
        with mock.patch.object(space_tui, "wrap_cells", wraps=space_tui.wrap_cells) as wrap:
            self.review(screen, mock.Mock(return_value=analysis))
        self.assertLess(wrap.call_count, 30)

    def test_issue_layout_keeps_all_text_and_reflows_on_resize(self):
        lines = [f"问题 {index}：中文路径 /long/path/" * 4 for index in range(20)]
        layout = space_tui._DetailLayout(lines)
        for width in (56, 112, 56):
            expected = [part for line in lines for part in space_tui.wrap_cells(line, width)]
            for requested in (0, 1, 35, len(expected) + 10, 2):
                offset, page = layout.page(width, requested, 12)
                target = min(requested, max(0, len(expected) - 12))
                self.assertEqual(offset, target)
                self.assertEqual(page, expected[target:target + 12])

    def test_partial_markers_follow_refresh_without_matching_sibling_names(self):
        entries = [SpaceEntry(Item(self.root / name, 0, "fixture", actionable=False), 0)
                   for name in ("data", "database")]
        calls = 0

        def analyzer(path, **_):
            nonlocal calls
            calls += 1
            target = "data" if calls == 1 else "database"
            return SpaceAnalysis(path, entries=entries, issues=[
                ScanIssue("permission_denied", "fixture denied", "analyze", path / target / "file"),
                ScanIssue("info", "nonblocking", "analyze", path / "database", blocking=False),
                ScanIssue("permission_denied", "outside", "analyze", path.parent / "data"),
            ])

        frames = []
        draw = space_tui._draw_browser

        def capture(screen, *args, **kwargs):
            before = len(screen.lines)
            draw(screen, *args, **kwargs)
            frames.append([line for line in screen.lines[before:] if "%" in line])

        with mock.patch.object(space_tui, "_draw_browser", side_effect=capture):
            self.review(_FakeScreen([ord("r"), ord("q")]), analyzer)
        self.assertEqual(calls, 2)
        self.assertIn("未测", frames[0][0])
        self.assertNotIn("未测", frames[0][1])
        self.assertNotIn("未测", frames[1][0])
        self.assertIn("未测", frames[1][1])

    def test_back_reuses_view_and_refresh_really_rescans(self):
        child = self.root / "child"
        child.mkdir()
        (child / "file").write_bytes(b"data")
        calls = []

        def traced(path, **kwargs):
            calls.append(path)
            return analyze_path(path, **kwargs)

        self.review(_FakeScreen([curses.KEY_RIGHT, curses.KEY_LEFT, ord("r"), ord("q")]), traced)
        self.assertEqual(calls, [self.root, child, self.root])

    def test_details_and_issues_preserve_selection(self):
        target = self.root / "file"
        target.write_bytes(b"data")
        screen = _FakeScreen([ord(" "), ord("i"), 27, ord("e"), 27, ord("d"), 10])
        result = self.review(screen)
        self.assertEqual([item.path for item in result.selected], [target])
        self.assertTrue(any("项目详情" in line for line in screen.lines))
        self.assertTrue(any("完整问题列表" in line for line in screen.lines))
        self.assertFalse(result.execution_confirmed)

    def test_navigation_error_survives_return_to_cached_parent(self):
        child = self.root / "child"
        child.mkdir()
        (child / "file").write_bytes(b"data")

        def analyzer(path, **kwargs):
            if path == child:
                raise AnalyzeError("fixture child unavailable")
            return analyze_path(path, **kwargs)

        screen = _FakeScreen([curses.KEY_RIGHT, ord("q")])
        self.review(screen, analyzer)
        self.assertTrue(any("fixture child unavailable" in line for line in screen.lines))

    def test_navigation_error_survives_return_to_uncached_parent(self):
        child = self.root / "child"
        child.mkdir()
        (child / "file").write_bytes(b"data")
        calls = []

        def analyzer(path, **kwargs):
            calls.append(path)
            if path == child:
                raise AnalyzeError("fixture child unavailable")
            return analyze_path(path, **kwargs)

        screen = _FakeScreen([curses.KEY_RIGHT, ord("q")])
        with mock.patch.object(space_tui, "SpaceCache", return_value=SpaceCache(max_entries=0)):
            self.review(screen, analyzer)
        self.assertEqual(calls, [self.root, child, self.root])
        self.assertTrue(any("fixture child unavailable" in line for line in screen.lines))

    def test_replaced_selection_is_revoked_before_confirmation(self):
        target = self.root / "file"
        target.write_bytes(b"data")

        class Screen(_FakeScreen):
            def getch(self):
                key = super().getch()
                if key == ord("d"):
                    target.rename(target.with_name("previous"))
                    target.write_bytes(b"data")
                return key

        screen = Screen([ord(" "), ord("d"), ord("q")])
        result = self.review(screen)
        self.assertTrue(result.cancelled)
        self.assertTrue(any("已撤销" in line for line in screen.lines))
        self.assertTrue(target.exists())
        self.assertFalse((self.home / ".Trash").exists())

    def test_successful_revalidation_replaces_old_incomplete_evidence(self):
        (self.root / "file").write_bytes(b"data")
        calls = 0

        def analyzer(path, **kwargs):
            nonlocal calls
            calls += 1
            result = analyze_path(path, **kwargs)
            if calls == 1:
                result.issues.append(ScanIssue("permission_denied", "old failure", "analyze", path))
            return result

        result = self.review(_FakeScreen([ord(" "), ord("d"), 10]), analyzer)
        self.assertTrue(result.submitted)
        self.assertTrue(result.complete)
        self.assertEqual(result.issues, ())

    def test_zero_rows_are_browse_only_and_hidden_from_classic_candidates(self):
        (self.root / "empty").mkdir()
        (self.root / "zero").touch()
        result = analyze_path(self.root, include_empty=True)
        self.assertEqual(result.entries, [])
        self.assertEqual({e.item.path.name for e in result.browse_entries}, {"empty", "zero"})
        self.assertTrue(all(not e.item.actionable for e in result.browse_entries))
        self.assertTrue(result.complete)

    def test_cache_rejects_replaced_root_including_replacement_before_put(self):
        (self.root / "file").write_bytes(b"data")
        analysis = analyze_path(self.root)
        cache = SpaceCache()
        cache.put(analysis)
        self.assertIsNotNone(cache.get(self.root))
        self.root.rename(self.home / "old")
        self.root.mkdir()
        self.assertIsNone(cache.get(self.root))
        cache.put(analysis)
        self.assertIsNone(cache.get(self.root))

    def test_cache_limits_and_cursor(self):
        cache = SpaceCache(max_views=1, max_entries=1)
        first = analyze_path(self.root)
        cache.put(first, cursor=3)
        self.assertEqual(cache.get(self.root).cursor, 3)
        second = self.home / "other"
        second.mkdir()
        cache.put(analyze_path(second))
        self.assertIsNone(cache.get(self.root))
        for name in ("a", "b"):
            (self.root / name).write_bytes(b"x")
        cache.put(analyze_path(self.root))
        self.assertIsNone(cache.get(self.root))

    def test_bounded_workers_keep_hardlink_results_equal(self):
        for index in range(24):
            (self.root / f"file-{index:02}").write_bytes(b"x" * (index + 1))
        os.link(self.root / "file-00", self.root / "alias")
        expected = analyze_path(self.root, workers=1)
        original = analyzer_module._scan_point
        lock = threading.Lock()
        active = maximum = 0

        def traced(*args, **kwargs):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            try:
                time.sleep(0.005)
                return original(*args, **kwargs)
            finally:
                with lock:
                    active -= 1

        updates = []
        with mock.patch.object(analyzer_module, "_scan_point", side_effect=traced):
            actual = analyze_path(self.root, workers=4, on_update=updates.append)
        self.assertGreater(maximum, 1)
        self.assertLessEqual(maximum, 4)
        self.assertEqual(actual.entries, expected.entries)
        self.assertEqual(actual.issues, expected.issues)
        self.assertTrue(updates)
        self.assertEqual(updates[-1].completed, 25)

    def test_cancel_keeps_already_measured_candidates(self):
        paths = [self.root / "first", self.root / "second"]
        for path in paths:
            path.write_bytes(b"data")
        control = engine.Control()
        original = engine._apply_scan_guards

        def cancel_after_first(item, guards, result):
            output = original(item, guards, result)
            control.cancel()
            return output

        with mock.patch.object(engine, "_apply_scan_guards", side_effect=cancel_after_first):
            snapshots = []
            result = engine.scan_points([ScanPoint("test", tuple(map(str, paths)))], ctl=control,
                                        workers=1, on_progress=snapshots.append)
        self.assertTrue(result.cancelled)
        self.assertFalse(result.complete)
        self.assertEqual([item.path for item in result.items], paths[:1])
        self.assertTrue(snapshots[-1].cancelled)
        self.assertLess(snapshots[-1].fraction, 1)

    def test_batching_does_not_duplicate_global_process_failure(self):
        root = self.home / "Library/Caches/com.openai.codex"
        root.mkdir(parents=True)
        for index in range(260):
            (root / f"file-{index}").write_bytes(b"x")
        with mock.patch("openclean.engine.capture_process_snapshot",
                        side_effect=ProcessDetectionError("fixture process query failed")) as capture:
            result = analyze_path(root)
        capture.assert_called_once()
        self.assertEqual(sum(i.code == "process_detection_failed" for i in result.issues), 1)
        self.assertTrue(all(not e.item.actionable for e in result.entries))

    def test_incomplete_submitted_review_returns_nonzero(self):
        class TTY(io.StringIO):
            def isatty(self):
                return True

        rules = self.home / "rules.json"
        rules.write_text('{"schema_version":1}')
        issue = ScanIssue("permission_denied", "denied", "analyze", self.root)
        review = SpaceReviewResult((), True, False, False, False, (issue,))
        with mock.patch("sys.stdin", TTY()), contextlib.redirect_stdout(TTY()), \
                contextlib.redirect_stderr(io.StringIO()), \
                mock.patch("openclean.cli.review_space", return_value=review):
            status = main(["analyze", str(self.root), "--rules", str(rules)])
        self.assertEqual(status, 1)

    def test_browser_failure_after_scan_does_not_restart_in_line_mode(self):
        (self.root / "file").write_bytes(b"fixture")

        class TTY(io.StringIO):
            def isatty(self):
                return True

        with mock.patch("sys.stdin", TTY()), contextlib.redirect_stdout(TTY()), \
                contextlib.redirect_stderr(io.StringIO()), mock.patch("curses.curs_set"), \
                mock.patch("curses.wrapper", side_effect=lambda fn: fn(_FakeScreen([]))), \
                mock.patch.object(space_tui, "_draw_browser", side_effect=curses.error("fixture draw error")), \
                mock.patch("openclean.cli.run_space_browser") as fallback:
            self.assertEqual(main(["analyze", str(self.root)]), 1)
        fallback.assert_not_called()

    def test_pause_requested_then_confirmed_at_real_checkpoint(self):
        gate = threading.Event()
        released = threading.Event()

        def no_checkpoint(path, *, control, **_):
            gate.set()
            released.wait(3)
            return SpaceAnalysis(path)

        job = AnalysisJob(self.root, engine.IgnoreRules(), analyzer=no_checkpoint)
        try:
            job.start()
            self.assertTrue(gate.wait(2))
            job.request_pause()
            # 阻塞 I/O 尚未到达检查点：只能显示“暂停已请求”，不能宣称已停住。
            self.assertFalse(job.paused)
        finally:
            released.set()
            job.close()

        checkpointed = threading.Event()

        def looping(path, *, control, **_):
            checkpointed.set()
            try:
                while True:
                    control.checkpoint()
                    time.sleep(0.001)
            except engine.Cancelled:
                return SpaceAnalysis(path, cancelled=True)

        job = AnalysisJob(self.root, engine.IgnoreRules(), analyzer=looping)
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

    def test_scan_page_pause_resume_and_completion_priority(self):
        started = threading.Event()
        finish = threading.Event()

        def slow(path, *, control, **_):
            started.set()
            while not finish.is_set():
                control.checkpoint()
                time.sleep(0.001)
            return SpaceAnalysis(path)

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
                if finish.is_set() and any("空间浏览" in line for line in self.lines):
                    return ord("q")
                time.sleep(0.01)
                return -1

        screen = Screen([])
        result = self.review(screen, slow)
        self.assertTrue(result.cancelled)
        self.assertTrue(any("已暂停" in line for line in screen.lines))
        self.assertTrue(any("Space 继续" in line for line in screen.lines))
        self.assertTrue(finish.is_set())

    def test_scan_to_review_boundary_discards_stale_space(self):
        completed = threading.Event()

        def fast(path, *, control, **_):
            completed.set()
            return SpaceAnalysis(path)

        class Screen(_FakeScreen):
            def getch(self):
                if not completed.wait(2):
                    raise AssertionError("worker 未启动")
                if self.keys:
                    return self.keys.pop(0)
                return -1

        screen = Screen([ord(" "), ord("q")])
        result = self.review(screen, fast)
        self.assertTrue(result.cancelled)
        # 扫描转审阅边界上的 Space 被清理，不会泄漏成第一次选择。
        self.assertFalse(any("已选择：" in line for line in screen.lines))

    def test_scan_candidates_report_current_processing_paths(self):
        paths = [self.root / "first", self.root / "second"]
        for path in paths:
            path.write_bytes(b"data")
        release = threading.Event()
        updates = []
        original = analyzer_module._scan_point

        def blocked(*args, **kwargs):
            kwargs["on_path"](paths[0])
            release.wait(2)
            return original(*args, **kwargs)

        def releaser():
            time.sleep(0.15)
            release.set()

        worker = threading.Thread(target=releaser)
        worker.start()
        try:
            with mock.patch.object(analyzer_module, "_scan_point", side_effect=blocked):
                analyzer_module._scan_candidates(
                    [str(path) for path in paths],
                    engine.Control(),
                    engine.IgnoreRules(),
                    1,
                    updates.append,
                    False,
                )
        finally:
            release.set()
            worker.join()
        self.assertTrue(updates)
        self.assertTrue(any(update.current_paths for update in updates))
        self.assertEqual(
            updates[0].current_paths, (str(paths[0]),)
        )
        self.assertTrue(all(len(update.current_paths) <= 1 for update in updates))


if __name__ == "__main__":
    unittest.main()
