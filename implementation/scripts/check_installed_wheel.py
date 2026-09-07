"""CI 中验证已安装 wheel 的 Agent 链路；候选和 Run 均在临时 HOME。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import openclean


def main() -> None:
    if "site-packages" not in Path(openclean.__file__).parts:
        raise RuntimeError("必须从独立 venv 的已安装 wheel 运行，不能导入 checkout")
    with tempfile.TemporaryDirectory(prefix="openclean-wheel-") as directory:
        home = Path(directory) / "home"
        target = home / ".codex/.tmp/marketplaces/.staging/marketplace-upgrade-synthetic"
        target.mkdir(parents=True)
        candidate = target / "blob"
        candidate.write_bytes(b"synthetic fixture")
        old = time.time() - 14 * 86400
        for path in (candidate, target):
            os.utime(path, (old, old))
        env = {**os.environ, "HOME": str(home), "XDG_STATE_HOME": str(home / ".local/state")}
        env.pop("PYTHONPATH", None)

        def call(*args: str) -> dict:
            result = subprocess.run(
                [sys.executable, "-m", "openclean", *args, "--json"],
                cwd=directory, env=env, capture_output=True, text=True, check=True,
                timeout=30,
            )
            return json.loads(result.stdout)

        assert call("strategy", "verify")["packs"][0]["strategy_count"] == 7
        run = call("inspect", "codex", "--home", str(home))
        assert run["complete"] and run["totals"]["actionable"] == 0
        selection = ("--run", run["run_id"], "--finding", run["findings"][0]["finding_id"])
        details = call("show", *selection)
        assert details["target"]["display_path"].startswith(str(home))
        plan = call("clean", *selection)
        assert plan["mode"] == "preview" and not plan["executed"]
        assert not plan["plan"]["plan_items"][0]["can_execute"]
        assert candidate.read_bytes() == b"synthetic fixture"
    print("PASS: installed wheel strategy/inspect/show/clean preview; temporary HOME only")


if __name__ == "__main__":
    main()
