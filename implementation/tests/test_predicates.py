from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from openclean.knowledge_base import KnowledgeBase
from openclean.models import FileFacts
from openclean.predicates import (
    AllPredicate,
    AnyPredicate,
    FileNamePredicate,
    FileSizePredicate,
    MissingPathPredicate,
    PathPatternPredicate,
    ProtectionGate,
)


@dataclass(frozen=True)
class _ConstantPredicate:
    value: bool

    def should_ignore(self, item: FileFacts) -> bool:
        return self.value


class _MustNotRunPredicate:
    def should_ignore(self, item: FileFacts) -> bool:
        raise AssertionError("知识库命中后不应继续求值")


class PredicateTests(unittest.TestCase):
    def test_all_and_any_short_circuit_semantics(self) -> None:
        item = FileFacts.from_path("/definitely/not/present")

        self.assertTrue(
            AllPredicate((_ConstantPredicate(True), _ConstantPredicate(True))).should_ignore(item)
        )
        self.assertFalse(
            AllPredicate((_ConstantPredicate(True), _ConstantPredicate(False))).should_ignore(item)
        )
        self.assertTrue(
            AnyPredicate((_ConstantPredicate(False), _ConstantPredicate(True))).should_ignore(item)
        )
        self.assertFalse(
            AnyPredicate((_ConstantPredicate(False), _ConstantPredicate(False))).should_ignore(item)
        )

    def test_concrete_file_predicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.log"
            path.write_bytes(b"12345")
            item = FileFacts.from_path(path)

            self.assertTrue(FileNamePredicate(("*.log",)).should_ignore(item))
            self.assertTrue(FileSizePredicate(min_bytes=10).should_ignore(item))
            self.assertTrue(FileSizePredicate(max_bytes=2).should_ignore(item))
            self.assertFalse(
                FileSizePredicate(min_bytes=1, max_bytes=10).should_ignore(item)
            )
            self.assertTrue(
                PathPatternPredicate(globs=(str(Path(tmp) / "*.log"),)).should_ignore(item)
            )
            self.assertTrue(
                PathPatternPredicate(regexes=(r"session\.log$",)).should_ignore(item)
            )
            self.assertFalse(MissingPathPredicate().should_ignore(item))
            self.assertTrue(
                MissingPathPredicate().should_ignore(
                    FileFacts.from_path(Path(tmp) / "missing")
                )
            )

    def test_knowledge_base_is_an_outer_short_circuit_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            protected = Path(tmp) / "protected"
            protected.write_text("do not scan", encoding="utf-8")
            knowledge_base = KnowledgeBase.from_mapping(
                {
                    "schema_version": 1,
                    "protect": {"paths": [str(protected)]},
                }
            )
            gate = ProtectionGate(
                knowledge_base=knowledge_base,
                predicates=(_MustNotRunPredicate(),),
            )

            self.assertTrue(gate.should_ignore(FileFacts.from_path(protected)))

    def test_empty_knowledge_base_precheck_avoids_path_work(self) -> None:
        gate = ProtectionGate()

        with mock.patch("openclean.predicates.normalize_path") as normalize:
            self.assertFalse(gate.knowledge_base_ignores("/unused/path"))

        normalize.assert_not_called()


if __name__ == "__main__":
    unittest.main()
