from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openclean.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseError,
    RulesFileNotFoundError,
)


class KnowledgeBaseTests(unittest.TestCase):
    def test_loads_and_queries_path_and_application_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            protected = root / "protected"
            ignored = root / "workspace" / "Cache"
            protected.mkdir()
            ignored.mkdir(parents=True)

            payload = {
                "schema_version": 1,
                "ignore": {
                    "globs": [str(root / "**" / "Cache")],
                    "regexes": [r"/temporary-[^/]+$"],
                },
                "protect": {"paths": [str(protected)]},
                "applications": {
                    "com.example.app": {
                        "name": "Example",
                        "protected": True,
                        "additional_files": [str(root / "Example.log")],
                        "deep_search": True,
                    }
                },
            }
            rules_path = root / "rules.json"
            rules_path.write_text(json.dumps(payload), encoding="utf-8")

            knowledge_base = KnowledgeBase.load(rules_path)

            self.assertTrue(
                knowledge_base.should_ignore_path(protected / "child" / "file")
            )
            self.assertTrue(knowledge_base.should_ignore_url(ignored.as_uri()))
            self.assertTrue(
                knowledge_base.should_ignore_path(root / "temporary-session")
            )
            self.assertFalse(
                knowledge_base.should_ignore_path(root / "protected-sibling")
            )
            self.assertTrue(knowledge_base.is_system_item(protected))
            self.assertTrue(knowledge_base.is_app_protected("com.example.app"))
            self.assertTrue(
                knowledge_base.is_deep_search_needed("com.example.app")
            )
            self.assertEqual(
                knowledge_base.application_name("com.example.app"), "Example"
            )
            self.assertEqual(
                knowledge_base.additional_files("Example", "com.example.app"),
                (root / "Example.log",),
            )

    def test_rejects_unknown_or_invalid_schema(self) -> None:
        with self.assertRaisesRegex(KnowledgeBaseError, "schema_version=1"):
            KnowledgeBase.from_mapping({"schema_version": 2})

        with self.assertRaisesRegex(KnowledgeBaseError, "未知字段"):
            KnowledgeBase.from_mapping(
                {"schema_version": 1, "ignore": {}, "typo": []}
            )

        with self.assertRaisesRegex(KnowledgeBaseError, "不是有效正则"):
            KnowledgeBase.from_mapping(
                {
                    "schema_version": 1,
                    "ignore": {"regexes": ["["]},
                }
            )

        with self.assertRaisesRegex(KnowledgeBaseError, "必须是绝对路径"):
            KnowledgeBase.from_mapping(
                {
                    "schema_version": 1,
                    "protect": {"paths": ["relative/path"]},
                }
            )

    def test_explicit_missing_rules_file_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.json"
            with self.assertRaises(RulesFileNotFoundError):
                KnowledgeBase.load_configured(missing)

    def test_managed_and_user_rules_merge_without_losing_custom_ignore(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            managed_path = root / "knowledge.json"
            user_path = root / "rules.json"
            managed_protected = root / "ManagedSystem"
            user_ignored = root / "ImportantProject"
            managed_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "protect": {"paths": [str(managed_protected)]},
                        "applications": {
                            "com.example.app": {
                                "name": "Managed",
                                "protected": True,
                                "additional_files": [
                                    str(root / "Managed.log")
                                ],
                                "deep_search": True,
                            }
                        },
                        "_managed": {"sequence": 4},
                    }
                ),
                encoding="utf-8",
            )
            user_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "ignore": {"paths": [str(user_ignored)]},
                        "applications": {
                            "com.example.app": {
                                "name": "User override",
                                "protected": False,
                                "additional_files": [str(root / "User.log")],
                                "deep_search": False,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch(
                "openclean.knowledge_base.DEFAULT_KNOWLEDGE_PATH",
                managed_path,
            ), mock.patch(
                "openclean.knowledge_base.DEFAULT_RULES_PATH",
                user_path,
            ):
                knowledge_base = KnowledgeBase.load_configured()

            self.assertTrue(
                knowledge_base.should_ignore_path(managed_protected / "child")
            )
            self.assertTrue(
                knowledge_base.should_ignore_path(user_ignored / "child")
            )
            self.assertTrue(knowledge_base.is_app_protected("com.example.app"))
            self.assertEqual(
                knowledge_base.application_name("com.example.app"),
                "User override",
            )
            self.assertTrue(
                knowledge_base.is_deep_search_needed("com.example.app")
            )
            self.assertEqual(
                knowledge_base.additional_files(
                    "User override",
                    "com.example.app",
                ),
                (root / "Managed.log", root / "User.log"),
            )

    def test_explicit_rules_file_does_not_implicitly_merge_managed_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            managed_path = root / "knowledge.json"
            explicit_path = root / "explicit.json"
            managed_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "protect": {"paths": [str(root / "Managed")]},
                    }
                ),
                encoding="utf-8",
            )
            explicit_path.write_text(
                json.dumps({"schema_version": 1}),
                encoding="utf-8",
            )

            with mock.patch(
                "openclean.knowledge_base.DEFAULT_KNOWLEDGE_PATH",
                managed_path,
            ):
                knowledge_base = KnowledgeBase.load_configured(explicit_path)

            self.assertFalse(
                knowledge_base.should_ignore_path(root / "Managed" / "child")
            )


if __name__ == "__main__":
    unittest.main()
