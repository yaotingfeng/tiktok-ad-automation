#!/usr/bin/env python3
"""本机统一发布入口：检查、推公司 main、构建上传、备份、发布及验收。"""

import argparse
import fcntl
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from itertools import pairwise
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy/release"))
from common import CONTEXT, VPC, require, write

COMPANY = "https://git.gzjunbo.net/yaotf/tiktok-ad-automation.git"
# 公司 main 已有更新提交、本批不再发布时的退出码；Jenkins 据此标记为未构建而非失败。
SUPERSEDED = 3


class Superseded(Exception):
    pass


def git(*args, cwd=ROOT):
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    require(result.returncode == 0, "Git operation failed: " + args[0])
    return result.stdout.strip()


def stage(args, cwd, env, log, abort_if=None):
    """运行一个阶段；abort_if 每分钟检查一次，返回真时终止该阶段并抛出 Superseded。"""
    # 完整输出只落入私有日志；控制台不展开运行配置或外部命令错误正文。
    print("执行：" + Path(args[0]).name + " " + Path(args[1]).name, flush=True)
    env = dict(env, RELEASE_DIAGNOSTIC_LOG=str(log.with_name("command-error.json")))
    with log.open("ab") as stream:
        process = subprocess.Popen(
            args,
            cwd=cwd,
            env=env,
            stdout=stream,
            stderr=stream,
        )
        while True:
            try:
                returncode = process.wait(timeout=60)
                break
            except subprocess.TimeoutExpired:
                if abort_if and abort_if():
                    # check.sh 收到 TERM 后自行回收测试容器与源码副本；保持同一进程组，
                    # 本机 Ctrl-C 仍能直接送达检查进程。
                    process.terminate()
                    try:
                        process.wait(timeout=180)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    raise Superseded("公司 main 在检查期间有了更新提交")
    require(returncode == 0, f"Step failed ({returncode}); private log: {log}")


def lane_summary(log):
    """检查线的启动/结束与耗时行不含配置，原样转到控制台，便于在 Jenkins 上看各线耗时。"""
    for line in log.read_text(errors="replace").splitlines():
        if line.startswith("检查线"):
            print(line, flush=True)


def release_summary(folder):
    """只公开阶段名和耗时；发布状态含私有配置，不能整份打印到 Jenkins。"""
    path = folder / "state.json"
    if not path.exists():
        return
    try:
        history = json.loads(path.read_text()).get("history", [])
        for previous, current in pairwise(history):
            phase = previous["phase"]
            if not re.fullmatch(r"[a-z][a-z-]*", phase):
                continue
            seconds = max(0, int(current["at"]) - int(previous["at"]))
            print(f"发布阶段耗时：{phase}（{seconds} 秒）", flush=True)
    except (OSError, ValueError, KeyError, TypeError):
        # 计时摘要不得覆盖原发布异常，也不能把异常里的私有内容带入控制台。
        print("发布阶段耗时：记录不完整，请查私有批次状态。", flush=True)


def remote_main(cwd):
    return git("ls-remote", "company", "refs/heads/main", cwd=cwd).split()[0]


def superseded(sha, remote, cwd):
    """sha 是公司 main 的严格祖先：已有更新提交在 main 上，本批发布已无意义。

    本机发布时公司 main 落后于 sha（随后快进推送），不算被取代。
    """
    if remote == sha:
        return False
    git("fetch", "-q", "company", "refs/heads/main", cwd=cwd)
    return (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", sha, remote], cwd=cwd, check=False
        ).returncode
        == 0
    )


def environment(config):
    env = dict(os.environ)
    env.update(
        KUBECONFIG=config["kubeconfig"],
        K8S_CONTEXT=CONTEXT,
        K8S_NAMESPACE="test",
        RETIRED_SOURCE_INVENTORY=config["retired_source_inventory"],
        PATH=os.pathsep.join(config.get("tool_paths", []) + [env.get("PATH", "")]),
    )
    for key in ("BUN_BIN", "PG_BIN_DIR"):
        if config.get(key):
            env[key] = config[key]
    for key in ("KUBECONFIG", "RETIRED_SOURCE_INVENTORY"):
        require(Path(env[key]).is_file(), key + " file is missing")
    return env


def prepare_checkout(folder):
    require(git("branch", "--show-current") == "main", "Run from local main")
    require(git("remote", "get-url", "company") == COMPANY, "Unexpected company remote")
    sha = git("rev-parse", "HEAD")
    require(re.fullmatch("[0-9a-f]{40}", sha), "Invalid main SHA")
    # 未提交文件始终留在原工作区；副本仅使用明确提交，绝不自动 git add。
    dirty = bool(git("status", "--porcelain", "--untracked-files=all"))
    if dirty:
        print("工作区存在未提交文件；本次仅使用已提交 main：" + sha, flush=True)
    checkout = folder / "checkout"
    git("clone", "--shared", "--no-checkout", str(ROOT), str(checkout))
    git("checkout", "--detach", sha, cwd=checkout)
    git("remote", "add", "company", COMPANY, cwd=checkout)
    return checkout, sha


def cleanup_checkout(folder):
    checkout = folder / "checkout"
    require(not checkout.is_symlink(), "Checkout cleanup cannot follow a symlink")
    if checkout.exists():
        shutil.rmtree(checkout)


def deployed_sha(env):
    """读取线上九个服务共用的镜像版本；无法确认时返回 None，检查随之全量。"""
    try:
        output = subprocess.run(
            [
                "kubectl",
                "--context",
                CONTEXT,
                "-n",
                "test",
                "get",
                "deployments",
                "-l",
                "app=tiktok-ad-automation",
                "-o",
                "json",
                "--request-timeout=30s",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        images = {
            item["spec"]["template"]["spec"]["containers"][0]["image"]
            for item in json.loads(output)["items"]
        }
    except subprocess.CalledProcessError, OSError, ValueError, KeyError, IndexError:
        return None
    if len(images) != 1:
        return None
    sha = next(iter(images)).removeprefix(VPC)
    return sha if re.fullmatch("[0-9a-f]{40}", sha) else None


def check_basis(base, sha, deployed, repo=ROOT):
    """最近一次已覆盖到线上版本的通过记录（本提交或其祖先）；没有则返回 None。

    记录含义：full 为从零全量通过；否则 base 到该提交之间的改动均已检查。只有
    覆盖起点与当前线上版本一致的记录，才能作为增量检查的起点。
    """
    best = None
    for path in sorted((base / "checks").glob("*.json")):
        record = json.loads(path.read_text())
        if not (record.get("full") or (deployed and record.get("base") == deployed)):
            continue
        candidate = record["sha"]
        if (
            candidate != sha
            and subprocess.run(
                ["git", "merge-base", "--is-ancestor", candidate, sha],
                cwd=repo,
                check=False,
            ).returncode
        ):
            continue
        distance = int(git("rev-list", "--count", candidate + ".." + sha, cwd=repo))
        if best is None or distance < best[0]:
            best = (distance, record)
    return best[1] if best else None


def plan_checks(args, base, folder, checkout, sha, env):
    """生成本次检查计划；返回 (计划, 通过后写入的覆盖记录)。"""
    command = [
        sys.executable,
        "deploy/ci/select_tests.py",
        "--repo",
        str(checkout),
        "--head",
        sha,
        "--output",
        str(folder / "check-plan.json"),
    ]
    deployed = deployed_sha(env)
    basis = None if args.full else check_basis(base, sha, deployed)
    if args.fast:
        command.append("--fast")
    if args.full:
        command.append("--full")
        coverage = {"full": True, "base": None}
    elif basis:
        # 增量起点已覆盖到线上版本：只检查起点之后的改动。
        command += ["--base", basis["sha"]]
        coverage = {"full": basis.get("full", False), "base": basis.get("base")}
    elif deployed:
        command += ["--base", deployed]
        coverage = {"full": False, "base": deployed}
    else:
        coverage = {"full": True, "base": None}
    subprocess.run(command, cwd=checkout, check=True)
    plan = json.loads((folder / "check-plan.json").read_text())
    if plan["mode"] == "full":
        coverage = {"full": True, "base": None}
    backend = plan["backend"]
    scope = "全部" if backend["all"] else f"{len(backend['targets'])} 个测试文件"
    lanes = [k for k, v in plan["frontend"].items() if v]
    print(
        f"检查范围：{plan['mode']}（{'；'.join(plan['reasons'])}）"
        + (f"；增量起点 {basis['sha'][:7]}" if basis else "")
        + (f"；线上 {deployed[:7]}" if deployed else "；线上版本未知")
        + f"；后端 {scope}；前端 {'/'.join(lanes) or '无'}"
        + ("；发布工具" if plan["deploy_tools"] else ""),
        flush=True,
    )
    return plan, coverage


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "check",
            "deploy",
            "backup",
            "backup-online",
            "status",
            "recover",
            "finish",
        ),
    )
    parser.add_argument(
        "--config", type=Path, default=ROOT / ".runtime/local-release/config.json"
    )
    parser.add_argument("--run", help="批次目录名；状态/恢复默认最近一批")
    parser.add_argument(
        "--full-backup",
        action="store_true",
        help="数据/任务合同变化时强制完整停写备份发布",
    )
    parser.add_argument(
        "--full", action="store_true", help="按需全量检查；默认只检查受改动影响的部分"
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="只跑快速层：跳过 slow 用例、全部验收和发布镜像集成；结果不作为发布依据",
    )
    parser.add_argument(
        "--full-scale",
        action="store_true",
        help="端到端验收按满规模运行（ACCEPTANCE_SCALE=full）；默认精简规模",
    )
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    base = args.config.resolve().parent
    env = environment(config)
    with (base / ".lock").open("a") as lock:
        try:
            if args.action != "status":
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another local release/check is running") from None
        if args.action in ("status", "recover", "finish"):
            run_name = (
                args.run or json.loads((base / "latest-run.json").read_text())["run"]
            )
            require(Path(run_name).name == run_name, "Invalid run name")
            folder = base / "runs" / run_name
            record = json.loads((folder / "run.json").read_text())
            if args.action == "status":
                state_path = folder / "state/state.json"
                state = (
                    json.loads(state_path.read_text()) if state_path.exists() else {}
                )
                print(
                    json.dumps(
                        {
                            "run": run_name,
                            "sha": record["sha"],
                            "phase": state.get("phase", record["phase"]),
                            "log": str(folder / "execution.log"),
                        },
                        ensure_ascii=False,
                    )
                )
                return
            stage(
                [
                    sys.executable,
                    "deploy/release/release.py",
                    args.action,
                    "--state-dir",
                    str(folder / "state"),
                ],
                folder / "checkout",
                env,
                folder / "execution.log",
            )
            cleanup_checkout(folder)
            print("恢复处理完成；请运行 status 查看状态。")
            return
        require(args.run is None, "--run is only valid for status/recover/finish")
        require(
            not args.full_backup or args.action == "deploy",
            "--full-backup is only valid for deploy",
        )
        require(
            not args.full or args.action in ("check", "deploy"),
            "--full is only valid for check/deploy",
        )
        require(
            not args.fast or args.action == "check", "--fast is only valid for check"
        )
        require(
            not args.full_scale or args.action in ("check", "deploy"),
            "--full-scale is only valid for check/deploy",
        )
        run_name = time.strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3)
        folder = base / "runs" / run_name
        folder.mkdir(parents=True, mode=0o700)
        checkout, sha = prepare_checkout(folder)
        record = {
            "sha": sha,
            "action": args.action,
            "phase": "prepared",
            "started_at": int(time.time()),
            "stages": [],
        }
        write(folder / "run.json", record)
        write(base / "latest-run.json", {"run": run_name})
        log = folder / "execution.log"

        def mark(phase):
            record["phase"] = phase
            record["stages"].append({"phase": phase, "at": int(time.time())})
            write(folder / "run.json", record)
            print("阶段：" + phase, flush=True)

        print("详细日志：" + str(log), flush=True)
        abort_if = None
        if args.action == "deploy":
            # 推送排队期间 main 可能已前进：先于检查发现就直接结束，不再白跑全量检查。
            started_remote = remote_main(checkout)
            if superseded(sha, started_remote, checkout):
                raise Superseded("公司 main 已有包含本提交的更新版本")

            def abort_if():
                # 偶发的远端查询失败不算被取代，下一分钟再查。
                try:
                    return remote_main(checkout) != started_remote
                except RuntimeError:
                    return False

        if args.action in ("check", "deploy"):
            plan, coverage = plan_checks(args, base, folder, checkout, sha, env)
            if plan["mode"] == "none":
                mark("checks-skipped")
            else:
                mark("checking")
                try:
                    stage(
                        ["bash", "deploy/ci/check.sh"],
                        checkout,
                        dict(
                            env,
                            CHECK_PLAN=str(folder / "check-plan.json"),
                            CHECK_CACHE_DIR=str(base / "ci-cache"),
                            **({"ACCEPTANCE_SCALE": "full"} if args.full_scale else {}),
                        ),
                        log,
                        abort_if,
                    )
                finally:
                    lane_summary(log)
                mark("checks-passed")
            # 记录本提交的覆盖范围，后续发布可在此基础上增量检查或直接复用。
            # 快速检查跳过了慢用例与验收，不能作为发布的增量起点，因此不记录。
            if plan["tier"] == "all":
                (base / "checks").mkdir(mode=0o700, exist_ok=True)
                write(
                    base / "checks" / (sha + ".json"),
                    {
                        "sha": sha,
                        **coverage,
                        "mode": plan["mode"],
                        "at": int(time.time()),
                    },
                )
        if args.action == "deploy":
            # 仅快进推送指定 SHA 到公司 main；origin 不参与发布。Jenkins 由推送触发，
            # 远端已是该 SHA，此时不再推送（Jenkins 只持有只读凭据）。
            mark("pushing")
            if (
                git("ls-remote", "company", "refs/heads/main", cwd=checkout).split()[0]
                != sha
            ):
                git("push", "company", sha + ":refs/heads/main", cwd=checkout)
            require(
                git("ls-remote", "company", "refs/heads/main", cwd=checkout).split()[0]
                == sha,
                "Company main changed during release",
            )
            mark("building")
            stage(["bash", "deploy/build-and-push.sh"], checkout, env, log)
        if args.action != "check":
            mark("releasing" if args.action == "deploy" else "backing-up")
            command = [
                sys.executable,
                "deploy/release/release.py",
                args.action,
                "--state-dir",
                str(folder / "state"),
            ]
            if args.action == "deploy":
                command += ["--sha", sha]
                if args.full_backup:
                    command.append("--full-backup")
            try:
                stage(command, checkout, env, log)
            finally:
                release_summary(folder / "state")
        mark("complete")
        cleanup_checkout(folder)
        print("完成：" + args.action + "；批次：" + run_name, flush=True)
        if args.action != "check":
            print(
                "数据库备份：/backup/latest；普通发布回退材料：/backup/latest-release（不含同一时点数据库）"
            )


if __name__ == "__main__":
    try:
        main()
    except Superseded as exc:
        print(
            "[local-release] 跳过：" + str(exc) + "；由最新提交的构建负责发布。",
            file=sys.stderr,
        )
        sys.exit(SUPERSEDED)
    except (RuntimeError, OSError, ValueError, KeyError) as exc:
        print("[local-release] " + str(exc), file=sys.stderr)
        print(
            "已停止；用 status 查看阶段。备份或迁移中断时保留批次目录，再按文档 recover/finish。",
            file=sys.stderr,
        )
        sys.exit(1)
