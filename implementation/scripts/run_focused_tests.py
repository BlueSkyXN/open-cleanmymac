"""定向发现并运行测试；零匹配不能视为验证通过。"""
from __future__ import annotations

import argparse
import sys
import unittest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pattern", help="tests 目录内的 unittest 文件匹配模式")
    args = parser.parse_args()
    suite = unittest.TestLoader().discover("tests", pattern=args.pattern)
    if suite.countTestCases() == 0:
        print(f"未发现测试：{args.pattern!r}；请检查 TEST_PATTERN。", file=sys.stderr)
        return 2
    result = unittest.TextTestRunner(verbosity=0).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
