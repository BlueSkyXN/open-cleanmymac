"""在 TemporaryDirectory 内测量 Codex 批量 Trash；进程/句柄使用合成空闲快照。"""
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import tempfile
import time
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from openclean.cleanup import execute_cleanup, select_cleanup_items
from openclean.codex_artifacts import scan_codex_transient_targets
from openclean.engine import IgnoreRules
from openclean.processes import OpenFileSnapshot, ProcessSnapshot


def benchmark(count: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="openclean-codex-batch-") as temporary:
        home = Path(temporary).resolve()
        for index in range(count):
            path = home / ".codex/.tmp" / f"git-{index:05d}"
            (path / "objects").mkdir(parents=True)
            (path / "refs").mkdir()
            (path / "HEAD").write_text("ref: refs/heads/main\n")
        observed = time.time() + 10 * 86400
        probes: Counter[str] = Counter()

        def runner(command, **_kwargs):
            if command[0] not in {"/bin/ps", "/usr/sbin/lsof"}:
                raise AssertionError("benchmark 不启动外部程序")
            probes[Path(command[0]).name] += 1
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch.dict(os.environ, {"HOME": str(home)}), \
                patch("openclean.codex_artifacts.time.time", return_value=observed):
            start = time.perf_counter()
            result = scan_codex_transient_targets(
                IgnoreRules(), home=home, process_snapshot=ProcessSnapshot(()),
                open_files=OpenFileSnapshot(()), now=observed,
            )
            scan_seconds = time.perf_counter() - start
            start = time.perf_counter()
            selected = select_cleanup_items(
                result.items, selectors=[str(item.path) for item in result.items], include_critical=True,
            )
            selection_seconds = time.perf_counter() - start
            start = time.perf_counter()
            report = execute_cleanup(selected, IgnoreRules(), home=home, process_runner=runner)
            execute_seconds = time.perf_counter() - start
            moved = sum(outcome.status == "moved_to_trash" for outcome in report.outcomes)
            source_left = len(list((home / ".codex/.tmp").iterdir()))
            trash_count = len(list((home / ".Trash").iterdir())) if (home / ".Trash").exists() else 0
        return {
            "targets": count, "scan_seconds": scan_seconds, "selection_seconds": selection_seconds,
            "execute_seconds": execute_seconds, "probe_calls": dict(probes),
            "complete": result.complete and report.complete and moved == count and source_left == 0 and trash_count == count,
            "moved_to_trash": moved, "source_remaining": source_left, "trash_targets": trash_count,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entries", type=int, nargs="+", default=[100, 1000])
    args = parser.parse_args()
    if any(count < 1 or count > 10000 for count in args.entries):
        parser.error("entries 必须在 1..10000 内")
    samples = [benchmark(count) for count in args.entries]
    print(json.dumps({
        "python": platform.python_version(), "system": platform.system(),
        "fixture": "TemporaryDirectory", "runtime_probes": "synthetic_idle_no_subprocess",
        "filesystem_action": "real_same_volume_trash", "samples": samples,
    }, indent=2))
    return 0 if all(sample["complete"] for sample in samples) else 1


if __name__ == "__main__":
    raise SystemExit(main())
