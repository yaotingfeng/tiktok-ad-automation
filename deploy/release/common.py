"""发布工具的私有文件、外部命令与固定目标。"""

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROJECT = "tiktok-ad-automation"
CONTEXT = "kubernetes-admin-c19dbd82590a34b9f87cebb8c1a858796"
PUBLIC = "sg-acr-prod-registry.ap-southeast-1.cr.aliyuncs.com/test/" + PROJECT + ":"
VPC = "sg-acr-prod-registry-vpc.ap-southeast-1.cr.aliyuncs.com/test/" + PROJECT + ":"
LEGACY_ROLES = (
    "api",
    "worker-resource",
    "worker-results",
    "worker-builds",
    "worker-control",
    "worker-ads-directory",
    "worker-ads-reporting",
    "worker-ad-management",
    "beat",
)
NEW_ROLES = ("worker-ads-backfill", "worker-ads-live-directory")
ROLES = (*LEGACY_ROLES, *NEW_ROLES)
FLAGS = (
    "MATERIAL_INGEST_ENABLED",
    "MATERIAL_CLEANUP_ENABLED",
    "ADS_SYNC_ENABLED",
    "AUTOMATED_RULES_ENABLED",
)


def mirrored(image):
    """公司 Jenkins 不能访问 Docker Hub，用 TKADA_IMAGE_MIRROR 指向本项目 ACR 副本；本机为空时仍用官方镜像。"""
    mirror = os.environ.get("TKADA_IMAGE_MIRROR", "")
    return mirror + image.replace("/", "-").replace(":", "-") if mirror else image


POSTGRES_IMAGE = mirrored("postgres:18")
REDIS_IMAGE = mirrored("redis:8")


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


# 国内发布机（公司 Jenkins）到新加坡集群 API 的连接会间歇断开，2026-10-29 实测短 exec
# 约 5–10% 失败，单次失败即中止整轮备份/发布。请求尚未送达集群的错误一律重试；
# 已送达与否不明确的断开只重试只读命令和调用方声明可重复执行的命令，有副作用的操作不重放。
KUBECTL_ATTEMPTS = 4
KUBECTL_RETRY_SECONDS = 5
KUBECTL_UNSENT = (
    "TLS handshake timeout",
    "couldn't get current server API group list",
    "error sending request",
    "dial tcp",
)
KUBECTL_INTERRUPTED = (
    "EOF",
    "connection reset by peer",
    "broken pipe",
    "i/o timeout",
    "http2: client connection lost",
)
KUBECTL_READ_ONLY = frozenset({"get", "wait", "version"})


def kubectl_retryable(args, stderr, *, idempotent=False):
    """判断 kubectl 失败是否属于可重试的连接中断；容器内命令自身失败绝不重试。"""
    if "command terminated with exit code" in stderr:
        return False
    if any(marker in stderr for marker in KUBECTL_UNSENT):
        return True
    read_only = (
        idempotent
        or "--dry-run=client" in args
        or next((a for a in args if not a.startswith("-")), "") in KUBECTL_READ_ONLY
    )
    return read_only and any(marker in stderr for marker in KUBECTL_INTERRUPTED)


def run(args, *, data=None, env=None, retryable=None):
    """retryable(stderr) 为真时按固定间隔重试，用于跨境 kubectl 的连接中断。"""
    for attempt in range(1, KUBECTL_ATTEMPTS + 1):
        result = subprocess.run(
            args, input=data, capture_output=True, env=env, check=False
        )
        if (
            not result.returncode
            or retryable is None
            or attempt == KUBECTL_ATTEMPTS
            or not retryable(result.stderr.decode(errors="replace"))
        ):
            break
        time.sleep(KUBECTL_RETRY_SECONDS * attempt)
    # 错误正文可能含私密连接配置，仅保存到调用方指定的私有诊断文件。
    if result.returncode and os.environ.get("RELEASE_DIAGNOSTIC_LOG"):
        write(
            os.environ["RELEASE_DIAGNOSTIC_LOG"],
            {
                "command": Path(args[0]).name,
                "returncode": result.returncode,
                "stderr": result.stderr.decode(errors="replace"),
            },
        )
    # 控制台只报动作，不回显正文。
    require(
        result.returncode == 0,
        f"{Path(args[0]).name} {args[1] if len(args) > 1 else ''} failed ({result.returncode})",
    )
    return result.stdout


def write(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = (
        value
        if isinstance(value, bytes)
        else (json.dumps(value, indent=2) + "\n").encode()
    )
    temp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class Cluster:
    def __init__(self):
        require(
            os.environ.get("K8S_CONTEXT") == CONTEXT,
            "K8S_CONTEXT must match the approved Singapore ACK cluster",
        )
        require(os.environ.get("K8S_NAMESPACE") == "test", "K8S_NAMESPACE must be test")
        self.base = ["kubectl", "--context", CONTEXT, "-n", "test"]

    def k(self, *args, data=None, idempotent=False):
        return run(
            self.base + ["--request-timeout=20m", *args],
            data=data,
            retryable=lambda stderr: kubectl_retryable(
                args, stderr, idempotent=idempotent
            ),
        )

    def get(self, kind, name=None):
        args = ["get", kind]
        args += [name] if name else ["-l", f"app={PROJECT}"]
        return json.loads(self.k(*args, "-o", "json"))

    def create(self, obj):
        return json.loads(
            self.k("create", "-f", "-", "-o", "json", data=json.dumps(obj).encode())
        )

    def scale(self, name, count):
        self.k("scale", "deployment/" + name, f"--replicas={count}")

    def deployments(self):
        items = {v["metadata"]["name"]: v for v in self.get("deployments")["items"]}
        require(
            set(items)
            in ({PROJECT + "-" + r for r in roles} for roles in (LEGACY_ROLES, ROLES)),
            "Expected the complete legacy nine or current eleven project Deployments",
        )
        for value in items.values():
            require(
                value["metadata"]["labels"]["app"] == PROJECT,
                "Deployment ownership mismatch",
            )
        return items

    def app_pods(self):
        return [
            p
            for p in self.get("pods")["items"]
            if p["metadata"].get("labels", {}).get("role") in ROLES
            and p.get("status", {}).get("phase") not in ("Succeeded", "Failed")
        ]
