#!/usr/bin/env python3
"""慢用例清单：按函数去参数、阈值筛选、验收目录不重复登记。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import slow_tests  # noqa: E402


class SlowTestsTests(unittest.TestCase):
    def test_parametrized_function_is_listed_once_when_any_instance_is_slow(self):
        durations = {
            "tests/a.py::test_x[1]": 0.2,
            "tests/a.py::test_x[2]": 12.0,
            "tests/a.py::test_y": 9.9,
            "tests/b.py::test_z": 10.0,
        }
        self.assertEqual(
            slow_tests.slow_functions(durations),
            ["tests/a.py::test_x", "tests/b.py::test_z"],
        )

    def test_acceptance_is_marked_by_directory_not_listed(self):
        durations = {"tests/acceptance/test_flow.py::test_big": 400.0}
        self.assertEqual(slow_tests.slow_functions(durations), [])


if __name__ == "__main__":
    unittest.main()
