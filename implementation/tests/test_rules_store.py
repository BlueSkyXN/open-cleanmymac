from __future__ import annotations

import contextlib
import io
import json
import stat
import tempfile
import unittest
from pathlib import Path

from openclean.cli import main
from openclean.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseError,
    RulesStore,
)
from openclean.models import normalize_path


class RulesStoreTests(unittest.TestCase):
    def test_cli_text_receipt_uses_the_persisted_normalized_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rules = root / "rules.json"
            relative = Path("relative") / ".." / "target"
            stdout = io.StringIO()

            with contextlib.chdir(root), contextlib.redirect_stdout(stdout):
                normalized = normalize_path(relative)
                status = main(
                    [
                        "ignore",
                        "add",
                        str(relative),
                        "--rules",
                        str(rules),
                    ]
                )

            self.assertEqual(status, 0)
            self.assertIn(str(normalized), stdout.getvalue())
            self.assertNotIn(str(relative), stdout.getvalue())
            self.assertEqual(
                RulesStore(rules).list_ignored_paths(),
                (normalized,),
            )

    def test_add_is_atomic_private_and_effective_for_descendants(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rules = root / "config" / "rules.json"
            ignored = root / "Projects" / "important"
            store = RulesStore(rules)

            self.assertTrue(store.add_ignored_path(ignored))

            self.assertEqual(store.list_ignored_paths(), (ignored,))
            self.assertEqual(stat.S_IMODE(rules.stat().st_mode), 0o600)
            self.assertTrue(
                KnowledgeBase.load(rules).should_ignore_path(ignored / "child")
            )
            self.assertEqual(
                sorted(path.name for path in rules.parent.iterdir()),
                ["rules.json"],
            )

    def test_parent_rule_replaces_redundant_children(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RulesStore(root / "rules.json")
            parent = root / "Projects"
            child = parent / "important"

            self.assertTrue(store.add_ignored_path(child))
            self.assertTrue(store.add_ignored_path(parent))
            self.assertFalse(store.add_ignored_path(child))
            self.assertEqual(store.list_ignored_paths(), (parent,))

    def test_mutation_preserves_non_path_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rules = root / "rules.json"
            payload = {
                "schema_version": 1,
                "ignore": {"globs": ["**/keep-*"], "regexes": ["safe$"]},
                "protect": {"paths": [str(root / "System")]},
                "applications": {
                    "com.example.app": {"protected": True}
                },
            }
            rules.write_text(json.dumps(payload), encoding="utf-8")
            store = RulesStore(rules)

            self.assertTrue(store.add_ignored_path(root / "Project"))

            updated = json.loads(rules.read_text(encoding="utf-8"))
            self.assertEqual(updated["ignore"]["globs"], ["**/keep-*"])
            self.assertEqual(updated["ignore"]["regexes"], ["safe$"])
            self.assertEqual(updated["protect"], payload["protect"])
            self.assertEqual(updated["applications"], payload["applications"])

    def test_remove_requires_an_exact_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RulesStore(root / "rules.json")
            parent = root / "Projects"
            store.add_ignored_path(parent)

            self.assertFalse(store.remove_ignored_path(parent / "child"))
            self.assertTrue(store.remove_ignored_path(parent))
            self.assertEqual(store.list_ignored_paths(), ())

    def test_invalid_existing_rules_are_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rules = Path(tmp) / "rules.json"
            rules.write_text("not-json", encoding="utf-8")
            store = RulesStore(rules)

            with self.assertRaises(KnowledgeBaseError):
                store.add_ignored_path(Path(tmp) / "Project")

            self.assertEqual(rules.read_text(encoding="utf-8"), "not-json")

    def test_cli_ignore_add_list_and_remove(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rules = root / "rules.json"
            ignored = root / "Project"

            add_stdout = io.StringIO()
            with contextlib.redirect_stdout(add_stdout):
                add_status = main(
                    [
                        "ignore",
                        "add",
                        str(ignored),
                        "--rules",
                        str(rules),
                        "--json",
                    ]
                )
            add_payload = json.loads(add_stdout.getvalue())
            self.assertEqual(add_status, 0)
            self.assertTrue(add_payload["changed"])
            self.assertEqual(add_payload["paths"], [str(ignored)])

            list_stdout = io.StringIO()
            with contextlib.redirect_stdout(list_stdout):
                list_status = main(
                    ["ignore", "list", "--rules", str(rules), "--json"]
                )
            self.assertEqual(list_status, 0)
            list_payload = json.loads(list_stdout.getvalue())
            self.assertEqual(list_payload["paths"], [str(ignored)])
            self.assertNotIn("changed", list_payload)

            remove_stdout = io.StringIO()
            with contextlib.redirect_stdout(remove_stdout):
                remove_status = main(
                    [
                        "ignore",
                        "remove",
                        str(ignored),
                        "--rules",
                        str(rules),
                        "--json",
                    ]
                )
            self.assertEqual(remove_status, 0)
            self.assertTrue(json.loads(remove_stdout.getvalue())["changed"])

            with contextlib.redirect_stdout(io.StringIO()):
                missing_status = main(
                    ["ignore", "remove", str(ignored), "--rules", str(rules)]
                )
            self.assertEqual(missing_status, 0)


if __name__ == "__main__":
    unittest.main()
