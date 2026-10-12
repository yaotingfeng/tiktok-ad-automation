#!/usr/bin/env python3
"""按实测耗时生成 backend/tests/slow_tests.txt：快速层据此排除慢用例。

耗时来自 backend_tests.py 写出的缓存（每条用例 setup+call+teardown 合计秒数）。
任一参数化实例达到阈值，整个测试函数视为慢用例：参数 ID 可能含随机值，按函数
登记才能在下次收集时稳定匹配。验收目录整体按目录标记为慢，不写入清单。
"""

import argparse
import json
import sys
from pathlib import Path

THRESHOLD_SECONDS = 10.0
ACCEPTANCE_PREFIX = "tests/acceptance/"
HEADER = (
    "# 快速层排除的慢用例（测试函数 ID，不含参数）。由 deploy/ci/slow_tests.py 按实测耗时生成；\n"
    "# tests/acceptance/ 整体按目录视为慢用例，不在此列出。\n"
)


def slow_functions(durations, threshold=THRESHOLD_SECONDS):
    return sorted(
        {
            nodeid.split("[", 1)[0]
            for nodeid, seconds in durations.items()
            if seconds >= threshold and not nodeid.startswith(ACCEPTANCE_PREFIX)
        }
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("durations", type=Path, nargs="+", help="耗时缓存 JSON，后者覆盖前者")
    parser.add_argument("--threshold", type=float, default=THRESHOLD_SECONDS)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "backend/tests/slow_tests.txt",
    )
    args = parser.parse_args()
    durations = {}
    for path in args.durations:
        durations.update(json.loads(path.read_text()))
    names = slow_functions(durations, args.threshold)
    args.output.write_text(HEADER + "".join(name + "\n" for name in names))
    print(f"慢用例 {len(names)} 个测试函数（阈值 {args.threshold:g} 秒）→ {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
