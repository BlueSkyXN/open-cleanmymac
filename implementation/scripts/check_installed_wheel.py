"""验证已安装 wheel 的 Agent 与经典精确清理链路，所有写入仅限临时 HOME。"""
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

        def call(*args: str, expected_status: int = 0) -> dict:
            result = subprocess.run(
                [sys.executable, "-m", "openclean", *args, "--json"],
                cwd=directory, env=env, capture_output=True, text=True,
                timeout=30,
            )
            if result.returncode != expected_status:
                raise RuntimeError(f"{args[0]}: exit {result.returncode}, expected {expected_status}; {result.stderr}")
            return json.loads(result.stdout)

        packs = {pack["name"]: pack["strategy_count"] for pack in call("strategy", "verify")["packs"]}
        assert packs["codex"] == 7 and packs["workbuddy"] == 1
        run = call("inspect", "codex", "--home", str(home))
        assert run["complete"] and run["totals"]["actionable"] == 0
        selection = ("--run", run["run_id"], "--finding", run["findings"][0]["finding_id"])
        details = call("show", *selection)
        assert details["target"]["display_path"].startswith(str(home))
        plan = call("clean", *selection)
        assert plan["mode"] == "preview" and not plan["executed"]
        assert not plan["plan"]["plan_items"][0]["can_execute"]
        assert candidate.read_bytes() == b"synthetic fixture"

        logs = home / ".workbuddy/logs/2026-01-01.expired-1700000000000-a1b2c3d4"
        logs.mkdir(parents=True)
        (logs / "fixture").write_bytes(b"keep workbuddy logs")
        run = call("inspect", "workbuddy")
        assert run["complete"] and run["totals"]["findings"] == 1
        assert run["totals"]["actionable"] == 0
        selection = ("--run", run["run_id"], "--finding", run["findings"][0]["finding_id"])
        assert call("show", *selection)["target"]["display_path"] == str(logs)
        assert not call("clean", *selection)["plan"]["plan_items"][0]["can_execute"]
        assert not call("clean", *selection, "--yes", "--include-critical", expected_status=1)["executed"]
        assert (logs / "fixture").read_bytes() == b"keep workbuddy logs"

        project = home / "Projects/demo"
        project.mkdir(parents=True)
        (project / "pyproject.toml").write_text("[project]\nname = 'fixture'\n", encoding="utf-8")
        for name in (".venv", ".mypy_cache"):
            artifact = project / name
            artifact.mkdir()
            data = artifact / "fixture"
            data.write_bytes(b"isolated artifact")
            for path in (data, artifact):
                os.utime(path, (old, old))
        discovered = call("purge", str(project))
        assert discovered["complete"]
        selected = next(item for group in discovered["projects"] for item in group["artifacts"]
                        if Path(item["path"]).name == ".venv")
        assert selected["actionable"] and selected["safety"] == "safe"
        arguments = ("purge", str(project), "--select", selected["path"])
        assert call(*arguments)["cleanup"] is None
        assert (project / ".venv/fixture").exists()
        executed = call(*arguments, "--yes")
        assert executed["complete"] and executed["cleanup"]["complete"]
        outcomes = executed["cleanup"]["outcomes"]
        assert len(outcomes) == 1 and outcomes[0]["status"] == "moved_to_trash"
        destination = Path(outcomes[0]["destination"])
        assert destination.is_relative_to(home / ".Trash")
        assert (destination / "fixture").read_bytes() == b"isolated artifact"
        assert (project / ".mypy_cache/fixture").exists()
        assert not (project / ".venv").exists()
        rescanned = call("purge", str(project))
        assert rescanned["complete"]
        assert all(Path(item["path"]).name != ".venv"
                   for group in rescanned["projects"] for item in group["artifacts"])
    print("PASS: installed wheel Codex/WorkBuddy inspect/show/preview/refusal and exact purge execution/rescan; temporary HOME only")


if __name__ == "__main__":
    main()
