"""在临时 HOME 内测量 Analyze；夹具创建不计时，不清空系统缓存。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import resource
import statistics
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from openclean.analyzer import analyze_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entries", type=int, default=10_000)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--shape", choices=("flat", "nested", "deep"), default="flat")
    parser.add_argument("--workers", type=int, default=None, help="仅新分析器的内部调度对照")
    args = parser.parse_args()
    if args.entries < 1 or args.repeat < 1 or (args.workers is not None and args.workers < 1):
        parser.error("entries / repeat / workers 必须大于 0")
    samples = []
    with tempfile.TemporaryDirectory(prefix="openclean-benchmark-") as directory:
        fixture = Path(directory).resolve()
        root = fixture / "data"
        root.mkdir()
        for index in range(args.entries):
            parent = root
            if args.shape == "nested":
                parent /= f"group-{index % 16}/part-{index % 128}"
            elif args.shape == "deep":
                parent = root.joinpath(*(f"d{n}" for n in range(1 + index % 48)))
            parent.mkdir(parents=True, exist_ok=True)
            (parent / f"file-{index}").write_bytes(b"x" * 1024)
        with patch.dict(os.environ, {"HOME": str(fixture)}):
            for _ in range(args.repeat):
                before = resource.getrusage(resource.RUSAGE_SELF)
                start = time.perf_counter()
                options = {} if args.workers is None else {"workers": args.workers}
                result = analyze_path(root, **options)
                elapsed = time.perf_counter() - start
                after = resource.getrusage(resource.RUSAGE_SELF)
                evidence = sorted(
                    (str(entry.item.path.relative_to(root)), entry.item.size,
                     entry.item.logical_size, entry.item.actionable, entry.item.action_block_reason)
                    for entry in result.entries
                )
                digest = hashlib.sha256(json.dumps(evidence, ensure_ascii=True).encode()).hexdigest()
                samples.append({
                    "seconds": elapsed, "user_seconds": after.ru_utime - before.ru_utime,
                    "system_seconds": after.ru_stime - before.ru_stime,
                    "maxrss": after.ru_maxrss, "complete": result.complete,
                    "items": len(result.entries), "bytes": result.total,
                    "issue_codes": sorted(issue.code for issue in result.issues),
                    "result_sha256": digest,
                })
    print(json.dumps({
        "python": platform.python_version(), "system": platform.system(),
        "machine": platform.machine(), "shape": args.shape, "entries": args.entries,
        "workers": args.workers, "cache_state": "newly-created, OS cache not cleared",
        "median_seconds": statistics.median(sample["seconds"] for sample in samples),
        "samples": samples,
    }, indent=2))
    return 0 if all(sample["complete"] for sample in samples) else 1


if __name__ == "__main__":
    raise SystemExit(main())
