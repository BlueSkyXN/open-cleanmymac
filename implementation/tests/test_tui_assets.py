from __future__ import annotations

import unittest
import xml.etree.ElementTree as ET

from scripts.capture_tui_assets import (
    ASSET_NAMES,
    DEFAULT_OUTPUT_DIR,
    render_assets,
)


class TuiAssetTests(unittest.TestCase):
    def test_committed_assets_match_safe_deterministic_renderer(self) -> None:
        if not DEFAULT_OUTPUT_DIR.is_dir():
            self.skipTest("仓库级 docs/assets 不包含在 sdist 中")

        first = render_assets()
        second = render_assets()

        self.assertEqual(first, second)
        self.assertEqual(tuple(first), ASSET_NAMES)
        for name, content in first.items():
            with self.subTest(name=name):
                ET.fromstring(content)
                self.assertEqual(
                    (DEFAULT_OUTPUT_DIR / name).read_text(encoding="utf-8"),
                    content,
                )

        combined = "\n".join(first.values())
        for expected in (
            "需逐项选择",
            "不可执行",
            "未指定 --yes",
            "显示最大 4/5 项",
            "云占位:12",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, combined)
        for forbidden in (
            "/Users/",
            "/Volumes/",
            "file://",
            "<script",
            "<foreignObject",
            "href=",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, combined)


if __name__ == "__main__":
    unittest.main()
