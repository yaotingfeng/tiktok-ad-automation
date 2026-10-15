#!/usr/bin/env python3
"""并行分组：均衡装箱、慢文件拆分、组内路径有序。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backend_tests  # noqa: E402


class PartitionTests(unittest.TestCase):
    def test_fast_files_stay_whole_and_sorted(self):
        ids = [f"tests/b.py::t{i}" for i in range(3)] + [f"tests/a.py::t{i}" for i in range(3)]
        groups = backend_tests.partition(ids, {}, 1)
        self.assertEqual(groups[0][1], ["tests/a.py", "tests/b.py"])

    def test_slow_file_is_split_across_groups(self):
        ids = [f"tests/acceptance/test_slow.py::t{i}" for i in range(4)] + ["tests/x.py::a"]
        durations = dict.fromkeys(ids[:4], 300.0)
        groups = backend_tests.partition(ids, durations, 4)
        slow_groups = [g for _, g in groups if any("test_slow.py::" in a for a in g)]
        self.assertEqual(len(slow_groups), 4)

    def test_load_is_balanced(self):
        ids = [f"tests/f{i}.py::t" for i in range(8)]
        durations = {nodeid: float(10 + i) for i, nodeid in enumerate(ids)}
        loads = [load for load, _ in backend_tests.partition(ids, durations, 4)]
        self.assertLessEqual(max(loads) - min(loads), 2.0)

    def test_unmeasured_acceptance_gets_heavy_weight(self):
        weights = backend_tests.weights_for(["tests/acceptance/test_x.py::a", "tests/y.py::b"], {})
        self.assertGreater(weights["tests/acceptance/test_x.py::a"], weights["tests/y.py::b"])

    def test_shard_urls_use_distinct_database_and_redis(self):
        env = {
            "DATABASE_URL": "postgresql://u:p@127.0.0.1:1/base_test",
            "REDIS_URL": "redis://127.0.0.1:2/0",
            "TEST_REDIS_URL": "redis://127.0.0.1:2/1",
        }
        first = backend_tests.shard_env(env, 0, "ci_shard0_test", Path("/tmp/a"))
        second = backend_tests.shard_env(env, 1, "ci_shard1_test", Path("/tmp/b"))
        self.assertTrue(first["DATABASE_URL"].endswith("/ci_shard0_test"))
        self.assertNotEqual(first["REDIS_URL"], second["REDIS_URL"])
        self.assertNotEqual(first["TEST_REDIS_URL"], second["TEST_REDIS_URL"])
        self.assertNotEqual(first["REDIS_URL"], first["TEST_REDIS_URL"])


    def test_acceptance_runs_in_exclusive_phase(self):
        ids = ["tests/x.py::a", "tests/acceptance/test_flow.py::b", "tests/y.py::c"]
        regular, exclusive = backend_tests.split_exclusive(ids)
        self.assertEqual(regular, ["tests/x.py::a", "tests/y.py::c"])
        self.assertEqual(exclusive, ["tests/acceptance/test_flow.py::b"])

    def test_fast_tier_deselects_slow_marker(self):
        fast = backend_tests.pytest_command(["tests"], fast=True)
        full = backend_tests.pytest_command(["tests"], fast=False)
        self.assertIn("not slow and not mcp", fast)
        self.assertIn("not mcp", full)
        self.assertFalse(any("slow" in arg for arg in full))

    def test_exclusive_shards_do_not_reuse_regular_resources(self):
        env = {
            "DATABASE_URL": "postgresql://u:p@127.0.0.1:1/base_test",
            "REDIS_URL": "redis://127.0.0.1:2/0",
            "TEST_REDIS_URL": "redis://127.0.0.1:2/1",
        }
        regular = backend_tests.shard_env(env, 5, "ci_shard5_test", Path("/tmp/a"))
        exclusive = backend_tests.shard_env(env, 6, "ci_shard6_test", Path("/tmp/b"))
        self.assertNotEqual(regular["REDIS_URL"], exclusive["REDIS_URL"])
        self.assertNotEqual(regular["TEST_REDIS_URL"], exclusive["TEST_REDIS_URL"])

if __name__ == "__main__":
    unittest.main()
