"""Run/Finding 标识测试（AR-04 §3）：稳定、唯一、不编码路径。"""
from __future__ import annotations

import unittest

from openclean.core.identifiers import new_finding_id, new_run_id
from openclean.core.models import ID_PREFIX_FINDING, ID_PREFIX_RUN


class IdentifierTests(unittest.TestCase):
    def test_prefixes(self) -> None:
        self.assertTrue(new_run_id().startswith(ID_PREFIX_RUN))
        self.assertTrue(new_finding_id().startswith(ID_PREFIX_FINDING))

    def test_uniqueness(self) -> None:
        runs = {new_run_id() for _ in range(2000)}
        findings = {new_finding_id() for _ in range(2000)}
        self.assertEqual(len(runs), 2000)
        self.assertEqual(len(findings), 2000)

    def test_ids_do_not_encode_paths(self) -> None:
        # 标识不得含路径分隔符或用户目录，避免从 ID 反推目标（决策 3）。
        for value in (new_run_id(), new_finding_id()):
            self.assertNotIn("/", value)
            self.assertNotIn("~", value)
            self.assertNotIn(".", value.split(":", 1)[1])
            token = value.split(":", 1)[1]
            self.assertEqual(len(token), 24)  # secrets.token_hex(12)
            int(token, 16)  # 必须是十六进制


if __name__ == "__main__":
    unittest.main()
