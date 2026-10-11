#!/usr/bin/env python3
"""按两次提交之间的改动挑选检查范围；无法判断的改动一律回退为全量。

后端按 Python 导入关系反向查找受影响的测试文件；聚合入口（app.main、
app.models 等会导入几乎所有模块）不向上传播，否则任何改动都会选中全部测试。
核心模块、迁移、依赖锁和测试基础设施的改动直接要求后端全量。
"""

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

# 这些模块被几乎所有代码导入；改动它们直接全量，经由它们的依赖不再向上传播。
HUBS = {
    "app.main",
    "app.models",
    "app.api.main",
    "app.api.deps",
    "app.jobs.celery_app",
}
# 改动即要求后端全量：运行时核心、调度、迁移、依赖与共用测试夹具。
BACKEND_FULL = (
    re.compile(r"^backend/app/(core|jobs|alembic)/"),
    re.compile(r"^backend/app/(main|models)\.py$"),
    re.compile(r"^backend/app/api/(deps|main)\.py$"),
    re.compile(r"^backend/(pyproject\.toml|alembic\.ini|Dockerfile)$"),
    re.compile(r"^(pyproject\.toml|uv\.lock|\.python-version)$"),
    re.compile(r"^backend/tests/(conftest|database|database_template|migration_database)\.py$"),
)
# 不影响任何检查结果的文件。
IGNORED = (
    re.compile(r"^docs/"),
    re.compile(r"(^|/)[^/]+\.md$"),
    re.compile(r"^(AGENTS|CLAUDE)\.md$"),
    re.compile(r"^\.(gitignore|editorconfig)$"),
)
# 端到端验收通过任务队列按名称调用 Worker，静态导入关系看不到；按业务区域显式对应。
_BUILD_FLOW = (
    "tests/acceptance/test_batch_flow.py",
    "tests/acceptance/test_isolation_recovery.py",
    "tests/acceptance/test_manual_links_flow.py",
    "tests/acceptance/test_runtime_diagnostics.py",
)
_R2_FLOW = (
    "tests/acceptance/test_r2_capacity.py",
    "tests/acceptance/test_r2_pipeline.py",
    "tests/acceptance/test_r2_probe.py",
    "tests/acceptance/test_r2_reconcile_cli.py",
)
ACCEPTANCE_BY_AREA = (
    (re.compile(r"^backend/app/modules/(builds|strategies|providers)/"), _BUILD_FLOW),
    (re.compile(r"^backend/app/modules/materials/"), _BUILD_FLOW + _R2_FLOW),
    (re.compile(r"^backend/app/modules/(accounts|tenants)/"), _BUILD_FLOW),
    (re.compile(r"^backend/app/integrations/"), _BUILD_FLOW + _R2_FLOW),
    (
        re.compile(r"^backend/app/modules/(reporting|ads|ad_management)/"),
        ("tests/acceptance/test_reporting_workspace.py",),
    ),
)
FRONTEND = re.compile(r"^(frontend/|bun\.lock$|package\.json$|biome\.json$)")
FRONTEND_BUILD_PAGES = re.compile(r"build", re.I)
FRONTEND_R2_PAGES = re.compile(r"(r2|upload|material|ingest)", re.I)
DEPLOY = re.compile(r"^(deploy/|scripts/|config/|\.env\.example$)")
# 发布集成测试需要构建镜像，只在发布编排、迁移或镜像本身变化时运行。
RELEASE_INTEGRATION = re.compile(
    r"^(deploy/release/|deploy/local\.py$|deploy/build-and-push\.sh$|backend/Dockerfile$"
    r"|backend/app/alembic/|scripts/test-release-integration\.py$)"
)


def empty_plan():
    return {
        "mode": "none",
        "reasons": [],
        "lint": False,
        "backend": {"all": False, "targets": []},
        "frontend": {"build": False, "workspace": False, "batch": False, "r2": False},
        "deploy_tools": False,
        "release_integration": False,
    }


def full_plan(reason):
    plan = empty_plan()
    plan.update(mode="full", reasons=[reason], lint=True, deploy_tools=True,
                release_integration=True)
    plan["backend"]["all"] = True
    plan["frontend"] = {"build": True, "workspace": True, "batch": True, "r2": True}
    return plan


def module_name(path: Path, backend: Path) -> str | None:
    relative = path.relative_to(backend).with_suffix("")
    parts = list(relative.parts)
    if parts[0] not in {"app", "tests", "scripts"}:
        return None
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def import_graph(backend: Path) -> tuple[dict[str, Path], dict[str, set[str]]]:
    """返回 模块→文件 与 模块→其导入的项目内模块。"""
    files: dict[str, Path] = {}
    for root in ("app", "tests", "scripts"):
        for path in (backend / root).rglob("*.py"):
            name = module_name(path, backend)
            if name:
                files[name] = path
    imports: dict[str, set[str]] = {name: set() for name in files}
    for name, path in files.items():
        package = name if path.name == "__init__.py" else name.rpartition(".")[0]
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    anchor = package.split(".")
                    anchor = anchor[: len(anchor) - (node.level - 1)]
                    base = ".".join(anchor + ([base] if base else []))
                targets = [base] + [f"{base}.{alias.name}" for alias in node.names]
            for target in targets:
                # 取存在的最长前缀：from a.b import c 可能是模块 a.b.c 或 a.b 的属性。
                while target and target not in files:
                    target = target.rpartition(".")[0]
                if target and target != name:
                    imports[name].add(target)
    return files, imports


def affected_tests(changed_modules: set[str], backend: Path) -> set[str]:
    files, imports = import_graph(backend)
    dependents: dict[str, set[str]] = {name: set() for name in files}
    for name, targets in imports.items():
        for target in targets:
            dependents[target].add(name)
    # conftest 作用于同目录及子目录全部测试：把它视为这些测试文件的依赖。
    conftests = {
        name: path.parent for name, path in files.items() if path.name == "conftest.py"
    }
    for conftest, folder in conftests.items():
        for name, path in files.items():
            if path.name.startswith("test_") and path.is_relative_to(folder):
                dependents[conftest].add(name)
    seen: set[str] = set()
    queue = [name for name in changed_modules if name in files]
    while queue:
        current = queue.pop()
        if current in seen:
            continue
        seen.add(current)
        if current in HUBS and current not in changed_modules:
            continue
        queue.extend(dependents[current] - seen)
    return {
        str(files[name].relative_to(backend))
        for name in seen
        if files[name].name.startswith("test_")
    }


def plan_for(changed: list[str], backend: Path) -> dict:
    plan = empty_plan()
    changed_modules: set[str] = set()
    for path in changed:
        if any(rule.search(path) for rule in IGNORED):
            continue
        if any(rule.search(path) for rule in BACKEND_FULL):
            return full_plan("核心/迁移/依赖/共用夹具改动：" + path)
        if path.startswith("backend/app/frontend/"):
            continue  # 前端构建产物，由前端检查覆盖。
        if path.startswith(("backend/app/", "backend/tests/", "backend/scripts/")):
            if not path.endswith(".py"):
                # 测试数据、协议样本等非代码文件：按所在目录的测试全部选中。
                folder = Path(path).parent
                plan["backend"]["targets"].extend(
                    str(p.relative_to(backend))
                    for p in (backend.parent / folder).rglob("test_*.py")
                )
                plan["lint"] = True
                continue
            name = module_name(backend.parent / path, backend)
            if name:
                changed_modules.add(name)
            for area, acceptance in ACCEPTANCE_BY_AREA:
                if area.search(path):
                    plan["backend"]["targets"].extend(acceptance)
            plan["lint"] = True
            continue
        if FRONTEND.search(path):
            plan["frontend"]["build"] = plan["frontend"]["workspace"] = True
            if path == "frontend/tests/acceptance-batch.spec.ts" or (
                path.startswith("frontend/src/") and FRONTEND_BUILD_PAGES.search(path)
            ):
                plan["frontend"]["batch"] = True
            if re.search(r"acceptance-r2|r2-", path) or (
                path.startswith("frontend/src/") and FRONTEND_R2_PAGES.search(path)
            ):
                plan["frontend"]["r2"] = True
            continue
        if DEPLOY.search(path):
            plan["deploy_tools"] = True
            if RELEASE_INTEGRATION.search(path):
                plan["release_integration"] = True
            continue
        return full_plan("无法归类的改动：" + path)
    tests = affected_tests(changed_modules, backend) if changed_modules else set()
    plan["backend"]["targets"] = sorted(set(plan["backend"]["targets"]) | tests)
    # 后端验收覆盖的业务链路，同时在浏览器中验证对应页面流程。
    selected = set(plan["backend"]["targets"])
    if "tests/acceptance/test_batch_flow.py" in selected or any(
        "acceptance/browser_server" in p for p in changed
    ):
        plan["frontend"]["batch"] = plan["frontend"]["build"] = True
    if any(t.startswith("tests/acceptance/test_r2_") for t in selected) or any(
        "acceptance/r2_browser_server" in p for p in changed
    ):
        plan["frontend"]["r2"] = plan["frontend"]["build"] = True
    if plan["backend"]["targets"] or plan["lint"] or plan["deploy_tools"] or any(
        plan["frontend"].values()
    ):
        plan["mode"] = "affected"
    plan["reasons"] = [f"{len(changed)} 个改动文件"]
    return plan


def changed_files(repo: Path, base: str, head: str) -> list[str]:
    output = subprocess.check_output(
        ["git", "diff", "--name-only", "--no-renames", f"{base}..{head}"],
        cwd=repo,
        text=True,
    )
    return [line for line in output.splitlines() if line]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--base", help="已验证的基准提交；缺省时全量")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.full:
        plan = full_plan("按需全量")
    elif not args.base:
        plan = full_plan("没有已验证的基准提交")
    else:
        try:
            changed = changed_files(args.repo, args.base, args.head)
        except subprocess.CalledProcessError:
            plan = full_plan("基准提交不在本地历史中：" + args.base)
        else:
            plan = plan_for(changed, args.repo / "backend")
    text = json.dumps(plan, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text + "\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
