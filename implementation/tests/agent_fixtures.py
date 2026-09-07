"""Synthetic test approval; never a production promotion or runtime option."""
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from openclean.actions.planner import execute_plan, resolve_plan
from openclean.cli import main
from openclean.core.models import Action, Conditions, Detector, Locator, Provenance, Strategy, StrategyAssessment, StrategyPack
from openclean.knowledge_base import KnowledgeBase
from openclean.predicates import ProtectionGate
from openclean.processes import OpenFileSnapshot, ProcessSnapshot
from openclean.runtime.inspect_service import inspect_target
from openclean.runtime.run_store import RunStore

from openclean.strategies.registry import StrategyRegistry, pack_hash


def approved_registry(packs):
    return StrategyRegistry(packs, approved_pack_hashes=frozenset(pack_hash(p) for p in packs))


EMPTY = (ProcessSnapshot(()), OpenFileSnapshot(()))


class AgentFixture(unittest.TestCase):
    """Every path and every write is inside one TemporaryDirectory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        env = mock.patch.dict(os.environ, {"HOME": str(self.home), "XDG_STATE_HOME": str(self.root / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.store = RunStore(self.root / "state/openclean/runs")
        self.protection = ProtectionGate(KnowledgeBase.empty())
        self.strategy = Strategy(
            id="codex.synthetic-staging", version=1, pack="codex", status="trusted",
            provenance=(Provenance("personal_experience", "obs:synthetic:test-only"),),
            locator=Locator(("~/.codex/.tmp/marketplaces/.staging",)),
            detector=Detector("codex_transient", {"subkind": "codex_marketplace_staging_targets"}),
            conditions=Conditions(minimum_age_days=7, require_no_open_handles=True),
            assessment=StrategyAssessment("cleanup_candidate", "high", "confirm"),
            action=Action("move_to_trash", True),
        )
        self.pack = StrategyPack("codex", (self.strategy,))
        self.registry = approved_registry((self.pack,))
        self.target = self.make_target("marketplace-upgrade-a")

    def make_target(self, name, days=8):
        path = self.home / ".codex/.tmp/marketplaces/.staging" / name
        path.mkdir(parents=True)
        (path / "blob").write_bytes(b"a" * 4096)
        old = time.time() - days * 86400
        os.utime(path / "blob", (old, old))
        os.utime(path, (old, old))
        return path

    def inspect(self, **kwargs):
        args = dict(registry=self.registry, run_store=self.store, home=self.home, snapshots=EMPTY)
        args.update(kwargs)
        result = inspect_target("codex", self.protection, **args)
        self.result = result
        return result

    def plan(self, result=None, **kwargs):
        result = result or self.result
        args = dict(run=result.run, findings=result.findings,
                    selected_ids=[f.finding_id for f in result.findings], registry=self.registry,
                    user_confirmed=True, include_confirm=True)
        args.update(kwargs)
        return resolve_plan(**args)

    def execute(self, plan, result=None, **kwargs):
        result = result or self.result
        args = dict(run=result.run, registry=self.registry, user_confirmed=True,
                    include_confirm=True, home=self.home, snapshots=EMPTY,
                    process_runner=lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "", ""))
        args.update(kwargs)
        return execute_plan(plan, result.findings, self.protection, **args)

    def cli(self, args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("openclean.runtime.inspect_service.capture_process_snapshot", return_value=EMPTY[0]), \
             mock.patch("openclean.runtime.inspect_service.capture_open_file_snapshot", return_value=EMPTY[1]), \
             redirect_stdout(out), redirect_stderr(err):
            code = main(args)
        self.assertEqual(err.getvalue(), "")
        return code, json.loads(out.getvalue())
