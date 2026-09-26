"""用临时目录和可协作停止的 worker 验证真实终端取消，不扫描真实 HOME。"""
from __future__ import annotations

import fcntl
import json
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from pathlib import Path


CHILD = '''
import os, sys, time
from openclean import cli, engine
def wait_for_cancel(root, control, *args, **kwargs):
    os.write(int(sys.argv[2]), b"started")
    while True:
        control.checkpoint()
        time.sleep(0.01)
engine._measure_dir = wait_for_cancel
options = ["analyze", sys.argv[1]]
if sys.argv[3] == "cli":
    options.append("--no-interactive")
raise SystemExit(cli.main(options))
'''

NAVIGATION_CHILD = '''
import json, os, sys
from openclean import cli, analyzer, space_tui
walks = 0
original_scan = analyzer._scan_candidates
original_draw = space_tui._draw_browser
def scan(*args, **kwargs):
    global walks
    walks += 1
    return original_scan(*args, **kwargs)
def draw(screen, analysis, *args, **kwargs):
    original_draw(screen, analysis, *args, **kwargs)
    os.write(int(sys.argv[2]), (json.dumps({"name": analysis.root.name, "walks": walks}) + "\\n").encode())
analyzer._scan_candidates = scan
space_tui._draw_browser = draw
space_tui._run_space_review.__kwdefaults__["revealer"] = lambda path: None
raise SystemExit(cli.main(["analyze", sys.argv[1]]))
'''


def probe_navigation():
    with tempfile.TemporaryDirectory() as raw:
        fixture = Path(raw).resolve()
        root = fixture / "data"
        (root / "child").mkdir(parents=True)
        (root / "child/file").write_bytes(b"data")
        marker_read, marker_write = os.pipe()
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 120, 0, 0))
        environment = dict(os.environ, HOME=str(fixture), TERM="xterm-256color",
                           PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        proc = subprocess.Popen([sys.executable, "-c", NAVIGATION_CHILD, str(root), str(marker_write)],
                                stdin=slave, stdout=slave, stderr=slave, env=environment,
                                pass_fds=(marker_write,), start_new_session=True)
        os.close(marker_write)
        frames = []
        buffer = b""
        deadline = time.monotonic() + 10
        back_sent = elapsed = None
        try:
            while len(frames) < 3:
                readable, _, _ = select.select([marker_read, master], [], [], 0.1)
                if master in readable:
                    os.read(master, 65536)
                if marker_read in readable:
                    chunk = os.read(marker_read, 4096)
                    if not chunk:
                        raise AssertionError("导航结束前子进程退出")
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        frames.append(json.loads(line))
                        if len(frames) == 1:
                            os.write(master, b"\x1bOC")
                        elif len(frames) == 2:
                            back_sent = time.monotonic()
                            os.write(master, b"\x1bOD")
                        elif len(frames) == 3:
                            elapsed = time.monotonic() - back_sent
                if time.monotonic() > deadline:
                    raise AssertionError("导航未完成")
            os.write(master, b"q")
            deadline = time.monotonic() + 5
            while proc.poll() is None:
                if select.select([master], [], [], 0.05)[0]:
                    os.read(master, 65536)
                if time.monotonic() > deadline:
                    raise AssertionError(f"Q 后导航未退出：{frames}")
            return {"frames": frames, "back_seconds": elapsed, "returncode": proc.returncode}
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            for fd in (master, slave, marker_read):
                os.close(fd)


def probe(mode, action):
    with tempfile.TemporaryDirectory() as raw:
        fixture = Path(raw).resolve()
        root = fixture / "data"
        (root / "directory").mkdir(parents=True)
        (root / "directory/file").write_bytes(b"data")
        marker_read, marker_write = os.pipe()
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 120, 0, 0))
        terminal_before = termios.tcgetattr(slave)
        environment = dict(os.environ, HOME=str(fixture), TERM="xterm-256color",
                           PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        started = time.monotonic()
        proc = subprocess.Popen(
            [sys.executable, "-c", CHILD, str(root), str(marker_write), mode],
            stdin=slave if mode == "tui" else subprocess.DEVNULL,
            stdout=slave, stderr=slave, env=environment, pass_fds=(marker_write,), start_new_session=True,
        )
        os.close(marker_write)
        output = bytearray()
        try:
            deadline = started + 10
            first_frame = None
            while True:
                readable, _, _ = select.select([marker_read, master], [], [], 0.1)
                if master in readable:
                    output.extend(os.read(master, 65536))
                    if first_frame is None and "正在扫描".encode() in output:
                        first_frame = time.monotonic() - started
                if marker_read in readable:
                    if not os.read(marker_read, 100):
                        raise AssertionError("worker 启动前子进程结束")
                    break
                if time.monotonic() > deadline:
                    raise AssertionError("worker 未启动")
            sent = time.monotonic()
            if action == "q":
                os.write(master, b"q")
            else:
                proc.send_signal(signal.SIGINT)
            while proc.poll() is None:
                if select.select([master], [], [], 0.05)[0]:
                    output.extend(os.read(master, 65536))
                if time.monotonic() - sent > 5:
                    raise AssertionError("取消后 worker 未结束")
            elapsed = time.monotonic() - sent
            while select.select([master], [], [], 0)[0]:
                output.extend(os.read(master, 65536))
            terminal_after = termios.tcgetattr(slave)
            # Darwin 从 cbreak 恢复规范模式时可能设置 PENDIN（待处理输入），
            # 它不是 echo / canonical / signal 模式的遗留变更。
            terminal_before[3] &= ~getattr(termios, "PENDIN", 0)
            terminal_after[3] &= ~getattr(termios, "PENDIN", 0)
            return proc.returncode, bytes(output), terminal_before == terminal_after, elapsed, first_frame
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            for fd in (master, slave, marker_read):
                os.close(fd)


@unittest.skipUnless(sys.platform == "darwin", "验证 macOS 原生 PTY")
class AnalyzePtyTests(unittest.TestCase):
    def test_back_in_real_terminal_does_not_rescan(self):
        result = probe_navigation()
        self.assertEqual(result["returncode"], 0)
        self.assertEqual([frame["name"] for frame in result["frames"]], ["data", "child", "data"])
        self.assertEqual([frame["walks"] for frame in result["frames"]], [1, 2, 2])

    def test_cancel_real_terminal_and_restore_modes(self):
        for mode, action, expected in (("cli", "SIGINT", 130), ("tui", "SIGINT", 130), ("tui", "q", 0)):
            with self.subTest(mode=mode, action=action):
                code, output, restored, _, _ = probe(mode, action)
                self.assertEqual(code, expected, output.decode(errors="replace"))
                self.assertNotIn(b"Traceback", output)
                self.assertTrue(restored)
                if mode == "tui":
                    self.assertIn("正在扫描".encode(), output)


if __name__ == "__main__":
    unittest.main()
