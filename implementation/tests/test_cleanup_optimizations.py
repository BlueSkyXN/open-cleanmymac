"""Behavior and work-count regressions for the second local cleanup pass."""
from __future__ import annotations

import contextlib
import io
import itertools
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openclean.analyzer import SpaceAnalysis, SpaceEntry
from openclean.cleanup import SelectionError, select_cleanup_items
from openclean.cli import _print_analyze_report, _select_analyze_items, main
from openclean.config import ConfigError, ConfigStore
from openclean.knowledge_base import KnowledgeBase, KnowledgeBaseError
from openclean.models import Item
from openclean.processes import ProcessSnapshot
from openclean.redaction import JsonPathRedactor


class RedactionOptimizationTests(unittest.TestCase):
    def test_longest_path_first_and_nested_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = str(Path(tmp) / "private folder")
            child = parent + "/缓存"
            payload = {
                "items": [
                    {"path": parent, "name": "private folder"},
                    {"path": child, "name": "缓存"},
                ],
                "note": f"{child} {parent}",
                "nested": {"paths": [child, parent]},
                "finding_ids": ["finding:example"],
                "message": "unknown path /elsewhere/private",
                "format": "items/second",
            }
            result = JsonPathRedactor().redact(payload)
            parent_ref, child_ref = (item["path"] for item in result["items"])
            self.assertNotEqual(parent_ref, child_ref)
            self.assertEqual(result["note"], f"{child_ref} {parent_ref}")
            self.assertEqual(result["nested"]["paths"], [child_ref, parent_ref])
            self.assertEqual(result["items"][1]["name"], child_ref)
            self.assertEqual(result["finding_ids"], ["finding:redacted"])
            self.assertEqual(result["message"], "[path details redacted]")
            self.assertEqual(result["format"], "items/second")
            self.assertNotIn(tmp, json.dumps(result))
            self.assertEqual(payload["items"][0]["path"], parent)

    def test_reused_redactor_rebuilds_references_for_each_document(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            earlier, later = str(Path(tmp) / "a"), str(Path(tmp) / "z")
            redactor = JsonPathRedactor()
            first = redactor.redact({"path": later, "note": later})
            first_ref = first["path"]
            second = redactor.redact({"paths": [earlier, later], "note": later})
            self.assertEqual(second["note"], second["paths"][1])
            self.assertNotEqual(first_ref, second["paths"][1])
            self.assertEqual(first["note"], first_ref)

    def test_root_alias_does_not_replace_slashes_in_ordinary_text(self) -> None:
        result = JsonPathRedactor(("/",)).redact({
            "root": "/", "format": "rows/second", "note": "nothing to replace",
        })
        self.assertRegex(result["root"], r"^path:\d+$")
        self.assertEqual(result["format"], "rows/second")
        self.assertEqual(result["note"], "nothing to replace")

    def test_replacement_order_is_sorted_once_per_document(self) -> None:
        with mock.patch("openclean.redaction.sorted", wraps=sorted, create=True) as sort:
            redactor = JsonPathRedactor()
            payload = {"items": [{"path": f"/synthetic/{i}", "note": "text"} for i in range(30)]}
            redactor.redact(payload)
            self.assertEqual(sort.call_count, 2)
            redactor.redact(payload)
            self.assertEqual(sort.call_count, 4)


class ProcessMatchingOptimizationTests(unittest.TestCase):
    def test_unicode_generator_markers_order_and_duplicate_commands(self) -> None:
        commands = ("Straße worker", "unrelated", "CODEX helper", "Straße worker")
        markers = (marker for marker in (" ", "STRASSE", " codex ", "missing"))
        result = ProcessSnapshot(commands).matching_commands(markers)
        self.assertEqual(result, (commands[0], commands[2], commands[3]))

    def test_casefold_is_computed_once_per_command(self) -> None:
        calls = []

        class CountedCommand(str):
            def casefold(self):
                calls.append(str(self))
                return super().casefold()

        commands = (CountedCommand("CODEX helper"), CountedCommand("idle process"))
        result = ProcessSnapshot(commands).matching_commands(("xcode", "trae", "codex"))
        self.assertEqual(result, (commands[0],))
        self.assertEqual(calls, list(commands))
        self.assertIs(result[0], commands[0])

    def test_empty_markers_do_not_evaluate_commands(self) -> None:
        class UnusedCommand(str):
            def casefold(self):
                raise AssertionError("Empty markers must not inspect commands")

        snapshot = ProcessSnapshot((UnusedCommand("not inspected"),))
        self.assertEqual(snapshot.matching_commands(iter(("", " \t"))), ())
        self.assertFalse(snapshot.any_running(()))


class SelectionOptimizationTests(unittest.TestCase):
    def test_bulk_selection_flags_keep_order_and_exclusions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            items = [
                Item(
                    path=Path(tmp) / str(index), size=index, category="selection",
                    safety=safety, preselected=preselected, actionable=actionable,
                    requires_explicit_selection=explicit,
                )
                for index, (safety, preselected, actionable, explicit) in enumerate(
                    itertools.product(("safe", "confirm", "critical"), (False, True),
                                      (False, True), (False, True))
                )
            ]
            for safe, confirm, critical in itertools.product((False, True), repeat=3):
                with self.subTest(safe=safe, confirm=confirm, critical=critical):
                    expected = [
                        item for item in items
                        if item.actionable and not item.requires_explicit_selection
                        and (item.preselected is True
                             or (safe and item.safety == "safe")
                             or (confirm and item.safety == "confirm")
                             or (critical and item.safety == "critical"))
                    ]
                    result = select_cleanup_items(
                        iter(items), select_all_safe=safe,
                        include_confirm=confirm, include_critical=critical,
                    )
                    self.assertEqual(result, expected)

    def test_explicit_selection_does_not_expand_to_other_tier_members(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            items = [Item(Path(tmp) / str(i), i, "selection", safety="confirm",
                          preselected=True) for i in range(3)]
            result = select_cleanup_items(items, selectors=(str(items[1].path),),
                                          include_confirm=True, include_critical=True)
            self.assertEqual(result, [items[1]])
            with self.assertRaisesRegex(SelectionError, "清理候选不唯一"):
                select_cleanup_items([items[1], items[1]], selectors=(str(items[1].path),),
                                     include_confirm=True)

    def test_analyze_selects_first_match_and_preserves_selector_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = Item(root / "one", 1, "first")
            duplicate = Item(root / "one", 2, "second", actionable=False)
            last = Item(root / "two", 3, "last")
            analysis = SpaceAnalysis(root, [SpaceEntry(item, 0) for item in (first, duplicate, last)])
            selected = _select_analyze_items(analysis, [str(last.path), str(first.path), str(last.path)])
            self.assertEqual(selected, [last, first])
            self.assertIs(selected[1], first)
            with self.assertRaisesRegex(SelectionError, "当前层级未找到分析候选"):
                _select_analyze_items(analysis, [str(root / "missing")])
            self.assertEqual(_select_analyze_items(analysis, []), [])

    def test_analyze_first_blocked_match_is_not_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blocked = Item(root / "same", 1, "first", actionable=False,
                           action_block_reason="fixture blocked")
            allowed = Item(root / "same", 1, "second")
            analysis = SpaceAnalysis(root, [SpaceEntry(item, 0) for item in (blocked, allowed)])
            with self.assertRaisesRegex(SelectionError, "fixture blocked"):
                _select_analyze_items(analysis, [str(blocked.path)])


class AnalyzeSummaryOptimizationTests(unittest.TestCase):
    def test_json_total_is_evaluated_once_and_includes_hidden_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entries = [SpaceEntry(Item(root / str(i), size, "summary"), 0)
                       for i, size in enumerate((20, 10))]
            analysis = SpaceAnalysis(root, entries)
            output = io.StringIO()
            with mock.patch.object(SpaceAnalysis, "total", new_callable=mock.PropertyMock,
                                   return_value=30) as total, contextlib.redirect_stdout(output):
                _print_analyze_report(analysis, True, top=1)
            self.assertEqual(total.call_count, 1)
            payload = json.loads(output.getvalue())
            for field in ("total_bytes", "potential_bytes", "allocated_bytes"):
                self.assertEqual(payload[field], 30)
            self.assertEqual(payload["reclaimable_bytes"], 0)
            self.assertTrue(payload["truncated"])
            self.assertEqual(payload["entry_count_total"], 2)
            self.assertEqual(payload["entry_count_returned"], 1)


class InvalidUtf8InputTests(unittest.TestCase):
    def test_config_errors_are_typed_and_corrupted_file_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_bytes(b"\xff\xfe")
            store = ConfigStore(path)
            for operation in (store.load, lambda: store.set_analytics(True)):
                with self.assertRaisesRegex(ConfigError, "UTF-8") as raised:
                    operation()
                self.assertIsInstance(raised.exception.__cause__, UnicodeDecodeError)
                self.assertEqual(path.read_bytes(), b"\xff\xfe")

    def test_rules_errors_are_typed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.json"
            path.write_bytes(b"\xff\xfe")
            with self.assertRaisesRegex(KnowledgeBaseError, "UTF-8") as raised:
                KnowledgeBase.load(path)
            self.assertIsInstance(raised.exception.__cause__, UnicodeDecodeError)
            self.assertEqual(path.read_bytes(), b"\xff\xfe")

    def test_cli_returns_json_errors_before_scan_or_inspect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "invalid.json"
            path.write_bytes(b"\xff\xfe")
            commands = (
                (["config", "--config-path", str(path), "--analytics", "on"], "config_error"),
                (["scan", "--rules", str(path)], "rules_error"),
                (["inspect", "codex", "--home", tmp, "--rules", str(path)], "rules_error"),
            )
            for (command, error_code), redact in itertools.product(commands, (False, True)):
                with self.subTest(command=command[0], redact=redact):
                    out, err = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                            mock.patch("openclean.cli.scan_domains") as scan, \
                            mock.patch("openclean.cli.inspect_target") as inspect:
                        status = main([*command, "--json", *(["--redact-paths"] if redact else [])])
                    self.assertEqual(status, 2)
                    payload = json.loads(out.getvalue())
                    self.assertEqual(payload["error"]["code"], error_code)
                    self.assertFalse(payload["executed"])
                    self.assertEqual(err.getvalue(), "")
                    self.assertEqual(path.read_bytes(), b"\xff\xfe")
                    scan.assert_not_called()
                    inspect.assert_not_called()
                    if redact:
                        self.assertNotIn(tmp, out.getvalue())


if __name__ == "__main__":
    unittest.main()
