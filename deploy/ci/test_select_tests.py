#!/usr/bin/env python3
"""检查范围选择：在临时仓库中构造导入关系，验证各类改动选中的检查。"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import select_tests  # noqa: E402

FILES = {
    "backend/app/__init__.py": "",
    "backend/app/main.py": "from app.modules.reporting import trends\nfrom app.modules.builds import execution\n",
    "backend/app/core/config.py": "",
    "backend/app/modules/__init__.py": "",
    "backend/app/modules/reporting/__init__.py": "",
    "backend/app/modules/reporting/trends.py": "from app.core import config\n",
    "backend/app/modules/reporting/queries.py": "from .trends import build\n",
    "backend/app/modules/builds/__init__.py": "",
    "backend/app/modules/builds/execution.py": "",
    "backend/tests/__init__.py": "",
    "backend/tests/conftest.py": "import app.main\n",
    "backend/tests/modules/reporting/test_queries.py": "from app.modules.reporting.queries import x\n",
    "backend/tests/modules/reporting/test_other.py": "import json\n",
    "backend/tests/modules/builds/conftest.py": "from tests.helpers import thing\n",
    "backend/tests/modules/builds/test_exec.py": "from app.modules.builds import execution\n",
    "backend/tests/helpers.py": "",
    "backend/tests/acceptance/test_batch_flow.py": "import app.main\n",
}


class SelectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        for name, text in FILES.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        self.backend = root / "backend"

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self, *changed):
        return select_tests.plan_for(list(changed), self.backend)

    def test_docs_only_needs_no_checks(self):
        plan = self.plan("docs/a.md", "AGENTS.md", "backend/README.md")
        self.assertEqual(plan["mode"], "none")

    def test_module_change_selects_reverse_dependents_without_hub_spread(self):
        plan = self.plan("backend/app/modules/reporting/trends.py")
        self.assertEqual(plan["mode"], "affected")
        # 经 app.main 聚合入口不向上传播，因此不会选中依赖 conftest/app.main 的全部测试。
        # 报表区域另按对照表附带报表端到端验收。
        self.assertEqual(
            plan["backend"]["targets"],
            [
                "tests/acceptance/test_reporting_workspace.py",
                "tests/modules/reporting/test_queries.py",
            ],
        )
        self.assertTrue(plan["lint"])
        self.assertFalse(plan["frontend"]["build"])

    def test_runtime_registered_area_adds_acceptance(self):
        plan = self.plan("backend/app/modules/builds/execution.py")
        targets = plan["backend"]["targets"]
        self.assertIn("tests/modules/builds/test_exec.py", targets)
        self.assertIn("tests/acceptance/test_batch_flow.py", targets)
        self.assertTrue(plan["frontend"]["batch"])

    def test_conftest_helper_change_selects_tests_under_its_folder(self):
        plan = self.plan("backend/tests/helpers.py")
        self.assertEqual(plan["backend"]["targets"], ["tests/modules/builds/test_exec.py"])

    def test_core_and_migrations_force_full(self):
        for path in (
            "backend/app/core/config.py",
            "backend/app/alembic/versions/x.py",
            "uv.lock",
            "backend/tests/conftest.py",
        ):
            self.assertEqual(self.plan(path)["mode"], "full", path)

    def test_frontend_and_deploy_lanes(self):
        plan = self.plan("frontend/src/routes/_layout/builds.tsx")
        self.assertTrue(plan["frontend"]["workspace"] and plan["frontend"]["batch"])
        self.assertEqual(plan["backend"]["targets"], [])
        plan = self.plan("deploy/release/backup.py")
        self.assertTrue(plan["deploy_tools"] and plan["release_integration"])
        plan = self.plan("scripts/test-k8s-deployment.py")
        self.assertTrue(plan["deploy_tools"])
        self.assertFalse(plan["release_integration"])

    def test_unknown_path_falls_back_to_full(self):
        self.assertEqual(self.plan("some/new/area.txt")["mode"], "full")


if __name__ == "__main__":
    unittest.main()
