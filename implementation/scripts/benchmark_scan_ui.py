"""临时 HOME + 真实 PTY 验收扫描交互；受控 worker，不是磁盘吞吐基准。"""
from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import pty
import select
import signal
import statistics
import struct
import subprocess
import sys
import tempfile
import termios
import time


CHILD = r'''
import os, signal, sys, threading, time
from pathlib import Path
from openclean import cli, engine
from openclean.scanpoints import ScanPoint
command, raw, marker = sys.argv[1:]
root = Path(raw)
signal_claimed = threading.Event()
signal_lock = threading.Lock()
def measurement(path, control, *args, **kwargs):
    os.write(int(marker), b'S\n')
    with signal_lock:
        send_from_worker = os.environ.get('OPENCLEAN_PROBE_WORKER_SIGINT') == '1' and not signal_claimed.is_set()
        if send_from_worker:
            signal_claimed.set()
    if send_from_worker:
        # 给协调线程时间进入结果等待，再把信号明确送达本工作线程。
        time.sleep(0.1)
        signal.pthread_kill(threading.get_ident(), signal.SIGINT)
    while True:
        control.checkpoint()
        time.sleep(0.001)
engine._measure_dir = measurement
def scan(*args, **kwargs):
    control = args[1]
    def scan_points():
        return engine.scan_points(
            [ScanPoint('fixture-' + name, (str(root / name),)) for name in ('a', 'b')],
            ctl=control, ignore=args[2], workers=2, on_progress=kwargs['on_progress'])
    if command == 'clean':
        # Clean 的协调路径经过任务图，Purge 直接等待扫描点。
        with control.delegating():
            outcome = engine.execute_task_graph(
                [engine.GraphTaskSpec('fixture', lambda: control.run_task(scan_points))],
                workers=1, on_abort=control.cancel).outcomes[0]
        if outcome.error is not None:
            raise outcome.error
        return outcome.value
    return scan_points()
if command == 'clean':
    cli.scan_domains = scan
    arguments = ['clean', 'dev', '--interactive']
elif command == 'purge':
    cli.scan_project_artifacts = scan
    arguments = ['purge', str(root), '--interactive']
else:
    arguments = ['analyze', str(root), '--interactive']
if os.environ.get('OPENCLEAN_UI_PROBE_MODE') == 'json':
    arguments[-1] = '--json'
os.write(int(marker), ('R' + str(time.monotonic()) + '\n').encode())
raise SystemExit(cli.main(arguments))
'''


def probe_json_cancel(command, *, signal_from_worker=False):
    """无 stdin/无 TTY 时，真实 SIGINT 仍必须及时返回一份可解析错误。"""
    with tempfile.TemporaryDirectory(prefix="openclean-json-cancel-") as raw:
        root = Path(raw).resolve()
        for name in ("a", "b"):
            (root / name).mkdir()
            (root / name / "file").write_bytes(b"fixture")
        read_fd, write_fd = os.pipe()
        env = dict(os.environ, HOME=str(root), OPENCLEAN_UI_PROBE_MODE="json",
                   OPENCLEAN_PROBE_WORKER_SIGINT="1" if signal_from_worker else "0",
                   PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        process = subprocess.Popen(
            [sys.executable, "-c", CHILD, command, str(root), str(write_fd)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, pass_fds=(write_fd,), start_new_session=True)
        os.close(write_fd)
        try:
            deadline = time.monotonic() + 5
            observed = b""
            while b"S\n" not in observed:
                if time.monotonic() >= deadline or not select.select([read_fd], [], [], .1)[0]:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("JSON worker 未启动")
                    continue
                chunk = os.read(read_fd, 4096)
                if not chunk:
                    raise RuntimeError("JSON worker 启动前进程结束")
                observed += chunk
            if not signal_from_worker:
                process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=5)
            try:
                payload = json.loads(stdout)
            except ValueError as exc:
                raise RuntimeError(
                    f"JSON 取消输出无法解析：{process.returncode}, {stdout!r}, {stderr!r}"
                ) from exc
            if process.returncode != 130 or payload.get("error", {}).get("code") != "cancelled" or stderr:
                raise RuntimeError(f"JSON 取消不符合契约：{process.returncode}, {payload}, {stderr!r}")
            return {"returncode": process.returncode, "json_valid": True, "stderr_empty": True}
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()
            os.close(read_fd)


def probe(command, *, action="q", size=(30, 110), smoke=False):
    with tempfile.TemporaryDirectory(prefix="openclean-pty-") as raw:
        home = Path(raw).resolve()
        root = home / "data"
        for name in ("a", "b"):
            (root / name).mkdir(parents=True)
            (root / name / "file").write_bytes(b"fixture")
        marker_read, marker_write = os.pipe()
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", *size, 0, 0))
        before = termios.tcgetattr(slave)
        env = dict(os.environ, HOME=str(home), TERM="xterm-256color",
                   PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        for name in ("LINES", "COLUMNS"):
            env.pop(name, None)
        started = time.monotonic()
        process = subprocess.Popen(
            [sys.executable, "-c", CHILD, command, str(root), str(marker_write)],
            stdin=slave, stdout=slave, stderr=slave, env=env,
            pass_fds=(marker_write,), start_new_session=True)
        os.close(marker_write)
        output = bytearray()
        markers = 0
        marker_buffer = b""
        ui_entry = None
        first_frame = pause_sent = pause_feedback = pause_confirmed = stop_sent = None
        deadline = started + 8
        stage = "start"
        tiny = size[0] < 14 or size[1] < 48
        needle = ("请放大终端窗口" if tiny else "正在扫描").encode()
        try:
            while process.poll() is None:
                if time.monotonic() > deadline:
                    raise RuntimeError(f"{command}/{action}/{size}: PTY 超时")
                ready, _, _ = select.select([master, marker_read], [], [], 0.01)
                if master in ready:
                    output.extend(os.read(master, 65536))
                if marker_read in ready:
                    marker_buffer += os.read(marker_read, 4096)
                    while b"\n" in marker_buffer:
                        event, marker_buffer = marker_buffer.split(b"\n", 1)
                        if event == b"S":
                            markers += 1
                        elif event.startswith(b"R"):
                            ui_entry = float(event[1:])
                now = time.monotonic()
                if first_frame is None and needle in output:
                    first_frame = now - started
                if stage == "start" and first_frame is not None:
                    if smoke:
                        stop_sent = now
                        os.write(master, b"q")
                        stage = "stop"
                    elif markers >= (1 if command == "analyze" else 2):
                        pause_sent = now
                        os.write(master, b" ")
                        stage = "pause"
                if stage == "pause":
                    if pause_feedback is None and any(
                        text.encode() in output for text in ("暂停已请求", "已暂停")
                    ):
                        pause_feedback = now - pause_sent
                    if "已暂停".encode() in output:
                        pause_confirmed = now - pause_sent
                        stop_sent = now
                        if action == "q":
                            os.write(master, b"q")
                        else:
                            process.send_signal(signal.SIGINT)
                        stage = "stop"
            stopped = time.monotonic()
            while select.select([master], [], [], 0)[0]:
                output.extend(os.read(master, 65536))
            expected = 0 if action == "q" or smoke else 130
            if process.returncode != expected or b"Traceback" in output:
                raise RuntimeError(f"{command}: exit={process.returncode}, {output.decode(errors='replace')}")
            if stop_sent is None or (not smoke and pause_confirmed is None):
                raise RuntimeError("缺少预期的扫描/暂停状态")
            after = termios.tcgetattr(slave)
            for state in (before, after):
                state[3] &= ~getattr(termios, "PENDIN", 0)
            if before != after:
                raise RuntimeError("终端模式未恢复")
            return dict(first_frame_ms=first_frame * 1000,
                        ui_entry_to_frame_ms=(started + first_frame - ui_entry) * 1000,
                        pause_feedback_ms=None if smoke else pause_feedback * 1000,
                        pause_confirmed_ms=None if smoke else pause_confirmed * 1000,
                        shutdown_ms=(stopped - stop_sent) * 1000,
                        returncode=process.returncode, terminal_restored=True)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            for descriptor in (master, slave, marker_read):
                os.close(descriptor)


def summarize(samples):
    result = {}
    for key in ("first_frame_ms", "ui_entry_to_frame_ms", "pause_feedback_ms", "pause_confirmed_ms", "shutdown_ms"):
        values = sorted(sample[key] for sample in samples)
        result[key] = dict(median=statistics.median(values),
                           p95=values[math.ceil(len(values) * .95) - 1], maximum=max(values))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.samples < 1:
        parser.error("samples 必须为正数")
    results = {"scope": "受控扫描 worker，含 Python 启动，真实 PTY；不是磁盘扫描速度", "cases": {}}
    for command in ("analyze", "clean", "purge"):
        for action in ("q", "SIGINT"):
            samples = [probe(command, action=action) for _ in range(args.samples)]
            results["cases"][f"{command}/{action}"] = {
                "count": len(samples), "summary": summarize(samples), "samples": samples}
            print(f"{command}/{action}: {len(samples)} samples passed", flush=True)
    results["window_sizes"] = {
        f"{command}/{width}x{height}": probe(command, size=(height, width), smoke=True)
        for command in ("analyze", "clean", "purge")
        for height, width in ((30, 110), (24, 80), (16, 60), (10, 40))}
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
    print(f"12 window scenarios passed; results: {args.output}")


if __name__ == "__main__":
    main()
