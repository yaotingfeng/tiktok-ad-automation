#!/usr/bin/env python3
"""ACK 发布入口；失败保留集群锁和本地状态，不自动回滚数据库。"""

import argparse
import base64
import copy
import hashlib
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path
from urllib.request import urlopen

from backup import (
    PROBE,
    cleanup_completed,
    local_destination,
    publish_local,
    restore_verify,
    snapshot,
)
from common import (
    CONTEXT,
    FLAGS,
    PROJECT,
    PUBLIC,
    ROOT,
    VPC,
    Cluster,
    require,
    run,
    write,
)

LOCK = PROJECT + "-release-lock"
WORKERS = {
    "worker-resource": "resources",
    "worker-results": "results",
    "worker-builds": "builds",
    "worker-control": "control",
    "worker-ads-directory": "ads-directory",
    "worker-ads-reporting": "ads-reporting",
    "worker-ad-management": "ad-management",
}


def drained(response, expected):
    return all(
        set(response.get(kind, {})) == set(expected)
        and not any(response[kind].values())
        for kind in ("active", "reserved", "scheduled", "active_queues")
    )


class Release:
    def __init__(self, folder):
        require(not Path(folder).is_symlink(), "State directory cannot be a symlink")
        self.folder = Path(folder).resolve()
        self.cluster = Cluster()
        self.state = {}
        self.values = {}
        self.timeout = int(os.environ.get("DRAIN_TIMEOUT_SECONDS", "1200"))

    def save(self, phase):
        self.state["phase"] = phase
        write(self.folder / "state.json", self.state)
        print("release phase: " + phase, flush=True)

    def assert_lock(self):
        obj = self.cluster.get("configmap", LOCK)
        require(
            obj["metadata"]["uid"] == self.state["lock_uid"]
            and obj["data"]["token"] == self.state["token"],
            "Release lock ownership changed",
        )

    def load(self):
        self.state = json.loads((self.folder / "state.json").read_text())
        require(
            self.state["context"] == CONTEXT and self.state["namespace"] == "test",
            "State belongs to another target",
        )
        self.assert_lock()
        if self.state["phase"] == "preparing":
            self.validate_deployments(original=True)
            return
        self.values = dict(
            line.split("=", 1)
            for line in (self.folder / "runtime.env").read_text().splitlines()
        )
        self.validate_deployments()

    def validate_deployments(self, original=False):
        current = self.cluster.deployments()
        for name, before in self.state["deployments"].items():
            require(
                current[name]["metadata"]["uid"] == before["metadata"]["uid"],
                "Deployment UID changed; recovery needs inspection",
            )
            if original:
                require(
                    current[name]["spec"]["template"] == before["spec"]["template"],
                    "Cannot resume old release after template changes",
                )
        return current

    def check_source(self, sha):
        run(["git", "-C", str(ROOT), "cat-file", "-e", sha + "^{commit}"])

    def archive_project(self, destination):
        write(
            destination,
            run(
                [
                    "git",
                    "-C",
                    str(ROOT),
                    "archive",
                    "--format=tar.gz",
                    self.state["old_sha"],
                ]
            ),
        )

    def begin(self, action):
        require(
            not self.folder.exists(),
            "Use a new private release directory; existing state must be recovered explicitly",
        )
        self.folder.mkdir(mode=0o700, parents=True)
        current = self.cluster.deployments()
        require(
            all(v["spec"]["replicas"] == 1 for v in current.values()),
            "Automatic release currently requires the approved one-replica topology",
        )
        images = {
            v["spec"]["template"]["spec"]["containers"][0]["image"]
            for v in current.values()
        }
        require(
            len(images) == 1 and next(iter(images)).startswith(VPC),
            "All roles must use the same approved VPC image",
        )
        old_sha = next(iter(images)).removeprefix(VPC)
        require(
            re.fullmatch("[0-9a-f]{40}", old_sha), "Current image must use a full SHA"
        )
        # 必须有旧源码才能生成独立项目归档；在停服前发现缺失。
        self.check_source(old_sha)
        self.state = {
            "context": CONTEXT,
            "namespace": "test",
            "token": secrets.token_hex(16),
            "action": action,
            "old_sha": old_sha,
            "deployments": current,
            "phase": "preparing",
        }
        lock = self.cluster.create(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {
                    "name": LOCK,
                    "labels": {"app": PROJECT, "role": "release-lock"},
                },
                "data": {
                    "token": self.state["token"],
                    "state_path": str(self.folder),
                    "created_at": str(int(time.time())),
                },
            }
        )
        self.state["lock_uid"] = lock["metadata"]["uid"]
        self.save("preparing")
        config = {"deployments": current, "secrets": {}}
        for suffix in ("runtime", "mcp-registration", "registry"):
            item = self.cluster.get("secret", PROJECT + "-" + suffix)
            require(
                item["metadata"]["labels"].get("app") == PROJECT,
                "Secret ownership mismatch",
            )
            config["secrets"][suffix] = item
        config["ingress"] = self.cluster.get("ingress", "test-https")
        for kind in ("services", "configmaps", "persistentvolumeclaims"):
            config[kind] = self.cluster.get(kind)
        write(self.folder / "cluster-before.json", config)
        self.values = {
            k: base64.b64decode(v).decode()
            for k, v in config["secrets"]["runtime"]["data"].items()
        }
        require(
            all("\n" not in v and "\r" not in v for v in self.values.values()),
            "Multiline runtime values are unsupported",
        )
        write(
            self.folder / "runtime.env",
            "".join(k + "=" + v + "\n" for k, v in self.values.items()).encode(),
        )
        write(
            self.folder / "mcp-client.json",
            base64.b64decode(
                config["secrets"]["mcp-registration"]["data"]["mcp-client.json"]
            ),
        )
        self.save("prepared")

    def env(self, sha):
        return dict(
            os.environ,
            RELEASE_ID=sha,
            APP_IMAGE=VPC + sha,
            LOCAL_IMAGE=PUBLIC + sha,
            INGRESS_HOST="tkada.shortclipflix.com",
            RUNTIME_ENV_FILE=str(self.folder / "runtime.env"),
            MCP_CLIENT_REGISTRATION_FILE=str(self.folder / "mcp-client.json"),
        )

    def check_remote(self, sha):
        require(re.fullmatch("[0-9a-f]{40}", sha), "Candidate SHA required")
        require(
            run(["git", "rev-parse", "HEAD"]).decode().strip() == sha,
            "Candidate SHA must equal HEAD",
        )
        require(
            not run(["git", "status", "--porcelain", "--untracked-files=all"]).strip(),
            "Release checkout must be clean",
        )
        remote = run(["git", "remote", "get-url", "company"]).decode().strip()
        require(
            remote == "https://git.gzjunbo.net/yaotf/tiktok-ad-automation.git",
            "Unexpected company remote",
        )
        latest = (
            run(["git", "ls-remote", "--exit-code", "company", "refs/heads/main"])
            .decode()
            .split()[0]
        )
        require(
            latest == sha,
            "A newer company main exists; skip this release before freezing",
        )

    def image_digest(self, sha):
        manifest = json.loads(
            run(
                [
                    "docker",
                    "buildx",
                    "imagetools",
                    "inspect",
                    "--format",
                    "{{json .Manifest}}",
                    PUBLIC + sha,
                ]
            )
        )
        if "manifests" in manifest:
            matches = [
                m["digest"]
                for m in manifest["manifests"]
                if m.get("platform", {}).get("architecture") == "amd64"
                and m.get("platform", {}).get("os") == "linux"
            ]
            require(
                len(matches) == 1, "Expected exactly one linux/amd64 image manifest"
            )
        # ACK/containerd报告的是仓库顶层index摘要，不能拿AMD64子清单与之比较。
        expected = manifest.get("digest", "")
        require(
            re.fullmatch(r"sha256:[0-9a-f]{64}", expected),
            "Registry image digest unavailable",
        )
        repo_digests = json.loads(
            run(
                [
                    "docker",
                    "image",
                    "inspect",
                    "--format",
                    "{{json .RepoDigests}}",
                    PUBLIC + sha,
                ]
            )
        )
        require(
            PUBLIC.rsplit(":", 1)[0] + "@" + expected in repo_digests,
            "Pulled image differs from the current registry digest; pull and recheck before release",
        )
        return expected

    def prepare_tools(self, sha=None):
        for image in {PUBLIC + self.state["old_sha"], "postgres:18", "redis:8"} | (
            {PUBLIC + sha} if sha else set()
        ):
            run(["docker", "pull", image])
        self.state["image_digests"] = {
            version: self.image_digest(version)
            for version in {self.state["old_sha"], sha or self.state["old_sha"]}
        }
        old_pods = self.cluster.app_pods()
        require(len(old_pods) == 9, "Existing rollout must be stable before release")
        for pod in old_pods:
            statuses = pod.get("status", {}).get("containerStatuses", [])
            require(
                len(statuses) == 1
                and statuses[0].get("ready")
                and statuses[0]
                .get("imageID", "")
                .endswith(self.state["image_digests"][self.state["old_sha"]]),
                "Current image tag no longer matches a ready running container",
            )
        self.save("prepared")
        # 普通发版不暗改 ConfigMap 默认值；这类变更需另行核对配置。
        before = json.loads((self.folder / "cluster-before.json").read_text())
        old_defaults = next(
            v["data"]
            for v in before["configmaps"]["items"]
            if v["metadata"]["name"] == PROJECT + "-defaults"
        )
        rendered = json.loads(
            self.cluster.k(
                "create",
                "--dry-run=client",
                "-f",
                str(ROOT / "deploy/k8s/tk-ada.yaml"),
                "-o",
                "json",
            )
        )
        new_defaults = next(
            v["data"] for v in rendered["items"] if v["kind"] == "ConfigMap"
        )
        require(
            old_defaults == new_defaults,
            "ConfigMap defaults changed; review configuration before automatic release",
        )
        # 停服前确认本机目标可写，无需云桶或额外备份云盘。
        local_destination()
        if sha:
            self.check_remote(sha)
        run(
            ["bash", str(ROOT / "deploy/k8s/apply.sh")],
            env=dict(self.env(sha or self.state["old_sha"]), PREFLIGHT_ONLY="1"),
        )

    def wait_pods(self, roles):
        deadline = time.monotonic() + self.timeout
        while any(
            p["metadata"]["labels"]["role"] in roles for p in self.cluster.app_pods()
        ):
            require(
                time.monotonic() < deadline,
                "Pods did not stop; inspect state before recovery",
            )
            time.sleep(3)

    def helper(self):
        template = copy.deepcopy(
            self.state["deployments"][PROJECT + "-beat"]["spec"]["template"]["spec"]
        )
        app = template["containers"][0]
        app["name"] = "app"
        app["command"] = ["python", "-c", "import time; time.sleep(86400)"]
        app.pop("args", None)
        for key in ("readinessProbe", "livenessProbe", "startupProbe"):
            app.pop(key, None)
        app["resources"] = {
            "requests": {"cpu": "25m", "memory": "128Mi"},
            "limits": {"cpu": "1", "memory": "1Gi"},
        }
        template["containers"] = [
            app,
            {
                "name": "postgres",
                "image": "postgres:18",
                "command": ["sleep", "86400"],
                "resources": {
                    "requests": {"cpu": "25m", "memory": "64Mi"},
                    "limits": {"cpu": "1", "memory": "1Gi"},
                },
            },
        ]
        template["restartPolicy"] = "Never"
        template["automountServiceAccountToken"] = False
        template["terminationGracePeriodSeconds"] = 15
        name = PROJECT + "-release-" + self.state["token"][:10]
        self.state["helper"] = name
        self.save("creating-helper")
        obj = self.cluster.create(
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {
                    "name": name,
                    "labels": {"app": PROJECT, "role": "release-helper"},
                    "annotations": {"release-token": self.state["token"]},
                },
                "spec": template,
            }
        )
        self.state["helper_uid"] = obj["metadata"]["uid"]
        self.save("helper-created")
        self.cluster.k("wait", "--for=condition=Ready", "pod/" + name, "--timeout=300s")

    def probe(self, action, expected=None):
        args = [
            "exec",
            "-i",
            self.state["helper"],
            "-c",
            "app",
            "--",
            "python",
            "-",
            action,
        ]
        if expected is not None:
            args.append(json.dumps(expected))
        return json.loads(self.cluster.k(*args, data=PROBE.read_bytes()))

    def pause(self):
        self.assert_lock()
        self.validate_deployments(original=True)
        self.save("stopping-producers")
        for role in ("beat", "api"):
            self.cluster.scale(PROJECT + "-" + role, 0)
        self.wait_pods({"api", "beat"})
        self.helper()
        pods = self.cluster.app_pods()
        expected = [
            WORKERS[p["metadata"]["labels"]["role"]] + "@" + p["metadata"]["name"]
            for p in pods
        ]
        require(
            len(expected) == 7 and len(set(expected)) == 7,
            "Exactly seven live workers are required",
        )
        self.state["workers"] = expected
        self.save("cancelling-consumers")
        self.probe("cancel", expected)
        self.save("draining")
        self.wait_drained(expected)
        self.save("stopping-workers")
        for role in WORKERS:
            self.cluster.scale(PROJECT + "-" + role, 0)
        self.wait_pods(set(WORKERS))
        broker = self.probe("broker")
        write(self.folder / "broker-frozen.json", broker)
        require(
            broker["unacked"] == 0,
            "Unacknowledged broker deliveries remain; inspect before backup",
        )
        self.save("frozen")

    def wait_drained(self, expected):
        deadline = time.monotonic() + self.timeout
        stable = 0
        while True:
            result = self.probe("inspect", expected)
            write(self.folder / "drain.json", result)
            stable = stable + 1 if drained(result, expected) else 0
            # inspect 的各项是分开采样；连续两次空闲避免 reserved 正移入 active 的窗口。
            if stable >= 2:
                return
            require(
                time.monotonic() < deadline,
                "Worker drain timed out; queues preserved, release stopped",
            )
            time.sleep(5)

    def remove_helper(self):
        if not self.state.get("helper"):
            return
        # 不删除被同名替换的 Pod。
        obj = self.cluster.get("pod", self.state["helper"])
        require(
            obj["metadata"]["annotations"].get("release-token") == self.state["token"],
            "Helper ownership changed",
        )
        self.cluster.k(
            "delete", "pod", self.state["helper"], "--wait=true", "--timeout=180s"
        )
        self.state.pop("helper", None)
        self.save(self.state["phase"])

    def config_unchanged(self):
        before = json.loads((self.folder / "cluster-before.json").read_text())
        for suffix in ("runtime", "mcp-registration"):
            require(
                self.cluster.get("secret", PROJECT + "-" + suffix)["data"]
                == before["secrets"][suffix]["data"],
                "Runtime configuration changed during release",
            )

    def resume(self, sha):
        self.assert_lock()
        current = self.validate_deployments()
        require(
            all(
                d["spec"]["template"]["spec"]["containers"][0]["image"] == VPC + sha
                for d in current.values()
            ),
            "Refusing to start an unexpected image",
        )
        self.config_unchanged()
        self.remove_helper()  # Beat RWO 磁盘先卸载，避免重启时多节点挂载冲突。
        self.save("resuming")
        for role in ("api", *WORKERS, "beat"):
            name = PROJECT + "-" + role
            self.cluster.scale(name, 1)
            self.cluster.k("rollout", "status", "deployment/" + name, "--timeout=10m")
        self.accept(sha)
        self.save("complete")
        self.unlock()
        cleanup_completed(self.folder)

    def accept(self, sha):
        current = self.validate_deployments()
        require(
            all(
                d["spec"]["template"]["spec"]["containers"][0]["image"] == VPC + sha
                for d in current.values()
            ),
            "Deployed image mismatch",
        )
        pods = self.cluster.app_pods()
        require(
            len(pods) == 9
            and {p["metadata"]["labels"]["role"] for p in pods}
            == {"api", "beat", *WORKERS},
            "Expected exactly one Pod for each of nine roles",
        )
        require(
            all(
                any(
                    c.get("type") == "Ready" and c.get("status") == "True"
                    for c in p.get("status", {}).get("conditions", [])
                )
                for p in pods
            ),
            "Some Pods are not ready",
        )
        expected_values = json.dumps({k: self.values[k] for k in FLAGS}, sort_keys=True)
        code = (
            "import json; from app.core.config import settings; print(json.dumps({k:str(getattr(settings,k)).lower() for k in "
            + repr(FLAGS)
            + "},sort_keys=True))"
        )
        before = json.loads((self.folder / "cluster-before.json").read_text())
        defaults = next(
            v["data"]
            for v in before["configmaps"]["items"]
            if v["metadata"]["name"] == PROJECT + "-defaults"
        )
        expected_env = dict(defaults, **self.values)
        expected_env.update(
            PYTHONUNBUFFERED="1",
            MCP_CLIENT_REGISTRATION_REF="/run/tk-ada/mcp-client.json",
        )
        config_hash = hashlib.sha256(
            json.dumps(expected_env, sort_keys=True).encode()
        ).hexdigest()
        registration_hash = hashlib.sha256(
            (self.folder / "mcp-client.json").read_bytes()
        ).hexdigest()
        config_code = (
            "import os,json,hashlib; from pathlib import Path; print(json.dumps([hashlib.sha256(json.dumps({k:os.environ.get(k) for k in "
            + repr(sorted(expected_env))
            + "},sort_keys=True).encode()).hexdigest(),hashlib.sha256(Path('/run/tk-ada/mcp-client.json').read_bytes()).hexdigest()]))"
        )
        for pod in pods:
            statuses = pod.get("status", {}).get("containerStatuses", [])
            require(
                len(statuses) == 1
                and statuses[0]
                .get("imageID", "")
                .endswith(self.state["image_digests"][sha]),
                "Running image digest differs from verified image",
            )
            hashes = json.loads(
                self.cluster.k(
                    "exec", pod["metadata"]["name"], "--", "python", "-c", config_code
                )
            )
            require(
                hashes == [config_hash, registration_hash],
                "Effective runtime or MCP registration differs",
            )
            result = (
                self.cluster.k(
                    "exec", pod["metadata"]["name"], "--", "python", "-c", code
                )
                .decode()
                .strip()
            )
            require(
                json.loads(result)
                == {k: str(v).lower() for k, v in json.loads(expected_values).items()},
                "Effective feature flags differ",
            )
        with urlopen(
            "https://tkada.shortclipflix.com/api/utils/health-check/", timeout=30
        ) as response:
            require(response.status == 200, "HTTPS health check failed")
        write(
            self.folder / "acceptance.json",
            {
                "sha": sha,
                "pods_ready": 9,
                "worker_readiness_checks": 7,
                "beat_count": 1,
                "flags": json.loads(expected_values),
                "https": True,
                "effective_config_sha256": config_hash,
                "image_digest": self.state["image_digests"][sha],
                "business_oauth_tested": False,
            },
        )

    def unlock(self):
        self.assert_lock()
        self.cluster.k("delete", "configmap", LOCK)

    def recover(self):
        self.load()
        if self.state["phase"] in ("preparing", "prepared"):
            self.validate_deployments(original=True)
            self.save("cancelled-before-freeze")
            self.unlock()
            return
        # migration-started 之后绝不自动启动旧应用或恢复数据库。
        require(
            self.state["phase"]
            not in ("migration-started", "candidate-held", "resuming", "complete"),
            "Inspect migration/acceptance state; automatic old-version recovery is forbidden",
        )
        self.validate_deployments(original=True)
        self.config_unchanged()
        # 取消消费可能只成功一部分；重建停用订阅的 Worker 前再次等待其在途任务。
        if self.state.get("workers") and self.state.get("helper"):
            pods = self.cluster.app_pods()
            expected = [
                WORKERS[p["metadata"]["labels"]["role"]] + "@" + p["metadata"]["name"]
                for p in pods
                if p["metadata"]["labels"]["role"] in WORKERS
            ]
            if expected:
                self.probe("cancel", expected)
                self.wait_drained(expected)
            for role in WORKERS:
                self.cluster.scale(PROJECT + "-" + role, 0)
            self.wait_pods(set(WORKERS))
        if "image_digests" not in self.state:
            self.state["image_digests"] = {
                self.state["old_sha"]: self.image_digest(self.state["old_sha"])
            }
        self.resume(self.state["old_sha"])

    def execute(self, action, sha):
        self.begin(action)
        self.prepare_tools(sha)
        if sha:
            self.state["candidate_sha"] = sha
            self.check_remote(sha)
        self.pause()
        snapshot(self)
        self.save("backed-up")
        restore_verify(self.folder, self.values, self.state["old_sha"], sha)
        publish_local(self.folder)
        self.save("backup-verified")
        if sha:
            self.assert_lock()
            self.config_unchanged()
            require(not self.cluster.app_pods(), "Application restarted during backup")
            self.save("migration-started")
            run(
                ["bash", str(ROOT / "deploy/k8s/apply.sh")],
                env=dict(self.env(sha), RELEASE_HOLD_WORKLOADS="1"),
            )
            self.save("candidate-held")
        self.resume(sha or self.state["old_sha"])


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("deploy", "backup", "recover", "status", "finish")
    )
    parser.add_argument(
        "--state-dir",
        required=True,
        help="Private persistent directory outside tracked source",
    )
    parser.add_argument("--sha", help="Exact published company main SHA for deploy")
    args = parser.parse_args()
    release = Release(args.state_dir)
    if args.action == "status":
        state = json.loads((release.folder / "state.json").read_text())
        print(
            json.dumps(
                {
                    k: state.get(k)
                    for k in ("phase", "action", "old_sha", "candidate_sha")
                }
            )
        )
    elif args.action == "recover":
        release.recover()
    elif args.action == "finish":
        release.load()
        require(
            release.state["phase"] in ("candidate-held", "resuming"),
            "Only a successfully applied candidate can be finished",
        )
        release.resume(release.state.get("candidate_sha", release.state["old_sha"]))
    else:
        require(args.action != "deploy" or args.sha, "--sha required for deploy")
        release.execute(args.action, args.sha if args.action == "deploy" else None)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, KeyError) as exc:
        print(f"[release] {type(exc).__name__}: {exc}", file=sys.stderr)
        print(
            "Release stopped. Preserve the private state directory; inspect status before recover/finish.",
            file=sys.stderr,
        )
        sys.exit(1)
