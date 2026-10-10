#!/usr/bin/env python3
"""本机统一发布入口：检查、推公司 main、构建上传、备份、发布及验收。"""

import argparse
import fcntl
import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy/release"))
from common import CONTEXT, require, write

COMPANY = "https://git.gzjunbo.net/yaotf/tiktok-ad-automation.git"


def git(*args, cwd=ROOT):
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    require(result.returncode == 0, "Git operation failed: " + args[0])
    return result.stdout.strip()


def stage(args, cwd, env, log):
    # 完整输出只落入私有日志；控制台不展开运行配置或外部命令错误正文。
    print("执行：" + Path(args[0]).name + " " + Path(args[1]).name, flush=True)
    with log.open("ab") as stream:
        result = subprocess.run(
            args, cwd=cwd, env=env, stdout=stream, stderr=stream, check=False
        )
    require(
        result.returncode == 0, f"Step failed ({result.returncode}); private log: {log}"
    )


def environment(config, base):
    env = dict(os.environ)
    env.update(
        KUBECONFIG=config["kubeconfig"],
        K8S_CONTEXT=CONTEXT,
        K8S_NAMESPACE="test",
        RETIRED_SOURCE_INVENTORY=config["retired_source_inventory"],
        LOCAL_BACKUP_DIR=str(base / "backups"),
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


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("check", "deploy", "backup", "status", "recover", "finish")
    )
    parser.add_argument(
        "--config", type=Path, default=ROOT / ".runtime/local-release/config.json"
    )
    parser.add_argument("--run", help="批次目录名；状态/恢复默认最近一批")
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    base = args.config.resolve().parent
    env = environment(config, base)
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
            print("恢复处理完成；请运行 status 查看状态。")
            return
        require(args.run is None, "--run is only valid for status/recover/finish")
        run_name = time.strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3)
        folder = base / "runs" / run_name
        folder.mkdir(parents=True, mode=0o700)
        checkout, sha = prepare_checkout(folder)
        record = {"sha": sha, "action": args.action, "phase": "prepared"}
        write(folder / "run.json", record)
        write(base / "latest-run.json", {"run": run_name})
        log = folder / "execution.log"
        if args.action in ("check", "deploy"):
            stage(["bash", "deploy/ci/check.sh"], checkout, env, log)
            record["phase"] = "checks-passed"
            write(folder / "run.json", record)
        if args.action == "deploy":
            # 仅快进推送指定 SHA 到公司 main；origin 不参与发布。
            git("push", "company", sha + ":refs/heads/main", cwd=checkout)
            require(
                git("ls-remote", "company", "refs/heads/main", cwd=checkout).split()[0]
                == sha,
                "Company main changed during release",
            )
            stage(["bash", "deploy/build-and-push.sh"], checkout, env, log)
        if args.action != "check":
            command = [
                sys.executable,
                "deploy/release/release.py",
                args.action,
                "--state-dir",
                str(folder / "state"),
            ]
            if args.action == "deploy":
                command += ["--sha", sha]
            stage(command, checkout, env, log)
        record["phase"] = "complete"
        write(folder / "run.json", record)
        print("完成：" + args.action + "；批次：" + run_name, flush=True)
        if args.action != "check":
            print("本地最新备份：" + str(base / "backups/latest.tar"))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, KeyError) as exc:
        print("[local-release] " + str(exc), file=sys.stderr)
        print(
            "已停止；用 status 查看阶段。备份或迁移中断时保留批次目录，再按文档 recover/finish。",
            file=sys.stderr,
        )
        sys.exit(1)
