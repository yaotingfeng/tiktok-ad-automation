"""保存 ACK 搭建诊断文件；只读 kubectl，输出目录必须在 Git 忽略范围内。"""

import argparse
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubectl", required=True)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--tenant", required=True, type=UUID)
    parser.add_argument("--submission", required=True, type=UUID)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--since", default="10m")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    if subprocess.run(
        check=False, args=["git", "check-ignore", "-q", str(output)], cwd=root
    ).returncode:
        parser.error("输出目录必须被 Git 忽略，避免提交真实运行日志")
    os.umask(0o077)
    output.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base = [args.kubectl, "--kubeconfig", args.kubeconfig, "-n", args.namespace]
    manifest = {
        "at": stamp,
        "tenant": str(args.tenant),
        "submission": str(args.submission),
        "files": [],
        "pods": [],
    }
    # 用 stdin 执行本地只读快照脚本；线上不写文件、不安装依赖。
    source = (root / "backend/scripts/build_runtime_snapshot.py").read_text()
    # 旧镜像没有新诊断模块，采集只需独立的错误类型而非注入代码。
    source = source.replace(
        "from app.core.diagnostics import error_fields",
        'def error_fields(error): return {"error_type": type(error).__name__}',
    )
    commands = [
        (
            "snapshot.json",
            [
                "exec",
                "-i",
                "deployment/tiktok-ad-automation-api",
                "--",
                "python",
                "-",
                "--tenant",
                str(args.tenant),
                "--submission",
                str(args.submission),
            ],
            source,
        )
    ]
    pods = subprocess.run(
        check=False,
        args=base + ["get", "pods", "-o", "json"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if pods.returncode == 0:
        for pod in json.loads(pods.stdout)["items"]:
            name = pod["metadata"]["name"]
            if any(
                name.startswith("tiktok-ad-automation-worker-" + role + "-")
                for role in ("builds", "resource", "results", "control")
            ):
                manifest["pods"].append(
                    {
                        "name": name,
                        "uid": pod["metadata"]["uid"],
                        "created_at": pod["metadata"]["creationTimestamp"],
                        "containers": [
                            {
                                "name": container["name"],
                                "image": container.get("image"),
                                "restarts": container.get("restartCount", 0),
                                **{
                                    field: {
                                        state: {
                                            key: value
                                            for key, value in info.items()
                                            if key
                                            in {
                                                "reason",
                                                "exitCode",
                                                "signal",
                                                "startedAt",
                                                "finishedAt",
                                            }
                                        }
                                        for state, info in container.get(
                                            field, {}
                                        ).items()
                                    }
                                    for field in ("state", "lastState")
                                },
                            }
                            for container in pod.get("status", {}).get(
                                "containerStatuses", []
                            )
                        ],
                    }
                )
                commands.append(
                    (
                        name + ".log",
                        ["logs", name, "--timestamps", "--since=" + args.since],
                        None,
                    )
                )
                for container in pod.get("status", {}).get("containerStatuses", []):
                    if container.get("restartCount", 0):
                        commands.append(
                            (
                                name + ".previous.log",
                                [
                                    "logs",
                                    name,
                                    "--previous",
                                    "--timestamps",
                                    "--since=" + args.since,
                                ],
                                None,
                            )
                        )
    failed = pods.returncode != 0
    for filename, command, content in commands:
        target = output / (stamp + "-" + filename)
        with target.open("w") as out:
            try:
                result = subprocess.run(
                    check=False,
                    args=base + command,
                    input=content,
                    text=True,
                    stdout=out,
                    stderr=subprocess.PIPE,
                    timeout=90,
                )
                status = result.returncode
                error_output = result.stderr
            except subprocess.TimeoutExpired as error:
                status = "timeout"
                error_output = error.stderr or "kubectl capture timed out"
                if isinstance(error_output, bytes):
                    error_output = error_output.decode(errors="replace")
        # 传输/Pod 切换失败也必须留证，不能只保存空快照。私有目录沿用0600。
        if error_output:
            target.with_suffix(target.suffix + ".stderr").write_text(error_output)
        failed = failed or status != 0
        manifest["files"].append(
            {"file": target.name, "exit": status, "bytes": target.stat().st_size}
        )
    (output / (stamp + "-manifest.json")).write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
