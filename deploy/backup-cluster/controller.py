"""集群一致性备份控制器；复用发布暂停状态机，不需要 Docker 或 Jenkins。"""

import argparse
import json
import os
import shutil
import socket
import sys
import tarfile
import time
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "release"))
from backup import PROBE, REDIS_RESTORE_CODE, snapshot, verify_archives
from common import CONTEXT, PROJECT, VPC, digest, require, run, write
from release import Release

NAME = PROJECT + "-backup"
ROOT = Path("/backups")


def in_cluster_config():
    account = Path("/var/run/secrets/kubernetes.io/serviceaccount")
    require(account.is_dir(), "This entrypoint only runs in the approved cluster")
    config = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [
            {
                "name": "ack",
                "cluster": {
                    "server": "https://"
                    + os.environ["KUBERNETES_SERVICE_HOST"]
                    + ":"
                    + os.environ.get("KUBERNETES_SERVICE_PORT", "443"),
                    "certificate-authority": str(account / "ca.crt"),
                },
            }
        ],
        "users": [{"name": "backup", "user": {"tokenFile": str(account / "token")}}],
        "contexts": [
            {
                "name": CONTEXT,
                "context": {"cluster": "ack", "user": "backup", "namespace": "test"},
            }
        ],
        "current-context": CONTEXT,
    }
    write("/tmp/backup-kubeconfig.json", config)
    os.environ.update(
        KUBECONFIG="/tmp/backup-kubeconfig.json",
        K8S_CONTEXT=CONTEXT,
        K8S_NAMESPACE="test",
    )


def restore_pod(name, image, node):
    # 恢复环境只接收解密历史凭据所需密钥，不注入真实数据库/R2/TikTok 连接配置。
    env = {
        "PROJECT_NAME": "TK-ADA backup verification",
        "DATABASE_URL": "postgresql+psycopg://postgres@127.0.0.1:5432/tkada_verify",
        "REDIS_URL": "redis://127.0.0.1:6379/0",
        "FIRST_SUPERUSER": "backup-check",
        "FIRST_SUPERUSER_PASSWORD": "isolated-validation-only",
        "SECRET_KEY": "isolated-validation-signing-key",
        "FASTAPI_ENV": "development",
        "MATERIAL_INGEST_ENABLED": "false",
        "MATERIAL_CLEANUP_ENABLED": "false",
        "ADS_SYNC_ENABLED": "false",
    }
    mount = [{"name": "backups", "mountPath": "/backups", "readOnly": True}]
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "labels": {"app": PROJECT, "role": "backup-restore"},
        },
        "spec": {
            "nodeName": node,
            "automountServiceAccountToken": False,
            "restartPolicy": "Never",
            "terminationGracePeriodSeconds": 10,
            "imagePullSecrets": [{"name": PROJECT + "-registry"}],
            "containers": [
                {
                    "name": "app",
                    "image": image,
                    "command": ["sleep", "86400"],
                    "env": [{"name": k, "value": v} for k, v in env.items()]
                    + [
                        {
                            "name": "CONNECTION_ENCRYPTION_KEY",
                            "valueFrom": {
                                "secretKeyRef": {
                                    "name": PROJECT + "-runtime",
                                    "key": "CONNECTION_ENCRYPTION_KEY",
                                }
                            },
                        }
                    ],
                    "volumeMounts": mount,
                    "resources": {
                        "requests": {"cpu": "25m", "memory": "128Mi"},
                        "limits": {"cpu": "1", "memory": "1Gi"},
                    },
                },
                {
                    "name": "postgres",
                    "image": "postgres:18",
                    "env": [
                        {"name": "POSTGRES_HOST_AUTH_METHOD", "value": "trust"},
                        {"name": "POSTGRES_DB", "value": "tkada_verify"},
                    ],
                    "args": ["postgres", "-c", "listen_addresses=127.0.0.1"],
                    "volumeMounts": mount,
                    "resources": {
                        "requests": {"cpu": "50m", "memory": "128Mi"},
                        "limits": {"cpu": "1", "memory": "1Gi"},
                    },
                },
                {
                    "name": "redis",
                    "image": "redis:8",
                    "args": [
                        "redis-server",
                        "--bind",
                        "127.0.0.1",
                        "--save",
                        "",
                        "--appendonly",
                        "no",
                    ],
                    "resources": {
                        "requests": {"cpu": "25m", "memory": "32Mi"},
                        "limits": {"cpu": "500m", "memory": "256Mi"},
                    },
                },
            ],
            "volumes": [
                {
                    "name": "backups",
                    "persistentVolumeClaim": {"claimName": NAME, "readOnly": True},
                }
            ],
        },
    }


class ClusterBackup(Release):
    def check_source(self, sha):
        # 归档实际运行镜像中的程序与前端构建；不依赖控制器持有 Gitea 账号。
        require(len(sha) == 40, "Unexpected current release")

    def archive_project(self, destination):
        shutil.copyfile(self.folder / "runtime-app.tar.gz", destination)

    def helper(self):
        # Beat 与工具 Pod 必须使用同一云盘节点，避免 RWO 多挂载。
        original = self.cluster.create

        def create(obj):
            obj["spec"]["nodeName"] = os.environ["NODE_NAME"]
            return original(obj)

        self.cluster.create = create
        try:
            super().helper()
        finally:
            self.cluster.create = original

    def prepare_tools(self, sha=None):
        require(sha is None, "Cluster backup does not deploy application versions")
        run(["age", "--version"])
        recipient = Path("/backup-public/recipient").read_text().strip()
        require(recipient.startswith("age1"), "Valid backup recipient required")
        require(
            shutil.disk_usage(ROOT).free > 2 * 1024**3,
            "Backup PVC has less than 2 GiB free",
        )
        pods = self.cluster.app_pods()
        require(len(pods) == 9, "Application rollout must be stable")
        ids = {
            p["status"]["containerStatuses"][0]["imageID"].split("@")[-1] for p in pods
        }
        require(
            len(ids) == 1
            and all(p["status"]["containerStatuses"][0].get("ready") for p in pods),
            "All nine roles must be ready on one image digest",
        )
        self.state["image_digests"] = {self.state["old_sha"]: ids.pop()}
        # 跨 namespace 旧消费者不能重启；当前部署前置资产核验记录仍保留。
        retired = json.loads(Path("/backup-public/retired-source.json").read_text())
        for item in retired["items"]:
            if item["kind"] == "Deployment":
                obj = json.loads(
                    run(
                        [
                            "kubectl",
                            "--context",
                            CONTEXT,
                            "-n",
                            retired["namespace"],
                            "get",
                            "deployment",
                            item["metadata"]["name"],
                            "-o",
                            "json",
                        ]
                    )
                )
                require(
                    obj["metadata"]["uid"] == item["metadata"]["uid"]
                    and obj["spec"]["replicas"] == 0
                    and not obj["status"].get("replicas"),
                    "Retired source consumer changed",
                )
        self.save("prepared")
        self.prepare_restore()

    def prepare_restore(self):
        name = NAME + "-verify-" + self.state["token"][:10]
        self.state["restore_pod"] = name
        self.save(self.state["phase"])
        obj = restore_pod(name, VPC + self.state["old_sha"], os.environ["NODE_NAME"])
        obj["metadata"]["annotations"] = {"backup-token": self.state["token"]}
        self.cluster.create(obj)
        self.cluster.k("wait", "--for=condition=Ready", "pod/" + name, "--timeout=300s")
        for _ in range(60):
            try:
                self.exec_restore(
                    "postgres", "pg_isready", "-h", "127.0.0.1", "-U", "postgres"
                )
                break
            except RuntimeError:
                time.sleep(1)
        else:
            raise RuntimeError("Restore PostgreSQL not ready")
        # 实测 NetworkPolicy 生效，不能仅因写了 YAML 就宣称隔离。
        targets = []
        for key in ("DATABASE_URL", "REDIS_URL"):
            url = urlsplit(self.values[key])
            ip = socket.gethostbyname(url.hostname)
            targets.append([ip, url.port or (5432 if key == "DATABASE_URL" else 6379)])
        targets.append(
            [
                os.environ["KUBERNETES_SERVICE_HOST"],
                int(os.environ.get("KUBERNETES_SERVICE_PORT", "443")),
            ]
        )
        code = (
            "import socket,json; targets="
            + repr(targets)
            + '\nfor host,port in targets:\n s=socket.socket();s.settimeout(2)\n try:\n  s.connect((host,port))\n except OSError: pass\n else: raise RuntimeError("Restore egress is not isolated")\n finally: s.close()\nprint("isolated")'
        )
        self.exec_restore("app", "python", "-c", code)
        write(
            self.folder / "isolation-verified.json",
            {
                "production_postgres_blocked": True,
                "production_redis_blocked": True,
                "kubernetes_api_blocked": True,
            },
        )

    def exec_restore(self, container, *args, data=None):
        return self.cluster.k(
            "exec",
            "-i",
            self.state["restore_pod"],
            "-c",
            container,
            "--",
            *args,
            data=data,
        )

    def verify(self):
        verify_archives(self.folder)
        self.exec_restore(
            "postgres",
            "psql",
            "-U",
            "postgres",
            "-d",
            "tkada_verify",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            'CREATE EXTENSION pg_trgm; CREATE EXTENSION "uuid-ossp";',
        )
        dump = str(self.folder / "postgres.dump")
        toc = self.exec_restore("postgres", "pg_restore", "--list", dump).decode()
        write(
            self.folder / "restore.toc",
            (
                "\n".join(
                    line for line in toc.splitlines() if " SCHEMA - public " not in line
                )
                + "\n"
            ).encode(),
        )
        self.exec_restore(
            "postgres",
            "pg_restore",
            "--use-list=" + str(self.folder / "restore.toc"),
            "--exit-on-error",
            "--single-transaction",
            "--no-owner",
            "--no-acl",
            "-U",
            "postgres",
            "-d",
            "tkada_verify",
            dump,
        )
        fingerprint = json.loads(
            self.exec_restore(
                "app", "python", "-", "fingerprint", data=PROBE.read_bytes()
            )
        )
        require(
            fingerprint
            == json.loads((self.folder / "source-fingerprint.json").read_text()),
            "Restored database differs from frozen source",
        )
        redis_result = json.loads(
            self.exec_restore(
                "app",
                "python",
                "-c",
                REDIS_RESTORE_CODE,
                data=(self.folder / "redis.json").read_bytes(),
            )
        )
        write(
            self.folder / "restore-verified.json",
            {
                "postgres_matches": True,
                "credentials_decrypted": fingerprint["credentials_decrypted"],
                "table_count": len(fingerprint["table_rows"]),
                "migration_heads": fingerprint["migration_heads"],
                "redis": redis_result,
                "verified_at": int(time.time()),
                "mode": "frozen",
                "storage": "cluster-pvc",
            },
        )
        self.save("backup-verified")

    def encrypt(self):
        manifest = json.loads((self.folder / "manifest.json").read_text())
        archive = self.folder / "backup.tar"
        with tarfile.open(archive, "w") as tar:
            for name in [
                *manifest["files"],
                "manifest.json",
                "restore-verified.json",
                "isolation-verified.json",
            ]:
                tar.add(self.folder / name, arcname=name)
        output = self.folder / "backup.tar.age"
        recipient = Path("/backup-public/recipient").read_text().strip()
        run(["age", "-r", recipient, "-o", str(output), str(archive)])
        with output.open("rb") as stream:
            os.fsync(stream.fileno())
        # 重新打开整文件核对持久化副本；磁盘副本不等于异地副本。
        expected = digest(output)
        require(expected == digest(output), "Encrypted archive reread failed")
        archive.unlink()
        write(
            self.folder / "encrypted.json",
            {
                "sha256": expected,
                "bytes": output.stat().st_size,
                "storage": "cluster-pvc",
                "offsite": False,
            },
        )
        self.save("encrypted")

    def cleanup_restore(self):
        name = self.state.get("restore_pod")
        if not name:
            return
        raw = self.cluster.k("get", "pod", name, "--ignore-not-found", "-o", "json")
        if not raw.strip():
            self.state.pop("restore_pod")
            self.save(self.state["phase"])
            return
        obj = json.loads(raw)
        require(
            obj["metadata"].get("annotations", {}).get("backup-token")
            == self.state["token"],
            "Restore Pod ownership changed",
        )
        self.cluster.k("delete", "pod", name, "--wait=true", "--timeout=180s")
        self.state.pop("restore_pod")
        self.save(self.state["phase"])

    def complete_archive(self):
        # 验收和加密都成功才清除明文；异常现场保留便于恢复。
        keep = {
            "backup.tar.age",
            "encrypted.json",
            "restore-verified.json",
            "acceptance.json",
            "isolation-verified.json",
        }
        write(
            self.folder / "completed.json",
            {
                "completed_at": int(time.time()),
                "sha": self.state["old_sha"],
                "offsite": False,
            },
        )
        keep.add("completed.json")
        for path in self.folder.iterdir():
            require(
                path.is_file() and not path.is_symlink(),
                "Unexpected private backup member",
            )
            if path.name not in keep:
                path.unlink()
        write(
            ROOT / "latest.json",
            {
                "directory": self.folder.name,
                **json.loads((self.folder / "completed.json").read_text()),
            },
        )

    def execute_backup(self):
        self.begin("cluster-backup")
        try:
            self.prepare_tools()
            self.pause()
            snapshot(self)
            self.save("backed-up")
            self.verify()
            self.encrypt()
            self.cleanup_restore()
            self.resume(self.state["old_sha"])
        except (RuntimeError, OSError, ValueError, KeyError):
            # 纯备份从不迁移/改配置。失败优先恢复服务，失败状态仍需报警。
            phase = self.state.get("phase")
            if phase not in ("complete", "resuming"):
                self.cleanup_restore()
                self.recover()
            raise
        self.complete_archive()
        prune(ROOT, int(os.environ.get("BACKUP_KEEP", "14")))


def prune(root, keep):
    require(keep >= 2, "Keep at least two verified backups")
    completed = []
    for folder in root.glob("backup-*"):
        if (
            folder.is_symlink()
            or not folder.is_dir()
            or not (folder / "completed.json").is_file()
        ):
            continue
        metadata = json.loads((folder / "completed.json").read_text())
        checksum = json.loads((folder / "encrypted.json").read_text())
        require(
            digest(folder / "backup.tar.age") == checksum["sha256"],
            "Existing encrypted backup was corrupted; retention stopped",
        )
        completed.append((metadata["completed_at"], folder))
    # 只清理由本工具成功创建的旧批次，不触碰失败现场和其他目录。
    for _, folder in sorted(completed, reverse=True)[keep:]:
        shutil.rmtree(folder)


def main():
    os.umask(0o077)
    in_cluster_config()
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("backup", "recover", "finish"))
    parser.add_argument("--state-dir")
    args = parser.parse_args()
    folder = (
        Path(args.state_dir)
        if args.state_dir
        else ROOT / ("backup-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
    )
    require(folder.parent == ROOT, "State must be directly under backup PVC")
    instance = ClusterBackup(folder)
    if args.action == "backup":
        instance.execute_backup()
    elif args.action == "recover":
        instance.load()
        instance.cleanup_restore()
        instance.recover()
    else:
        instance.load()
        require(
            instance.state["phase"] == "resuming",
            "Only interrupted service resumption may finish",
        )
        instance.resume(instance.state["old_sha"])
        if (folder / "encrypted.json").is_file():
            instance.complete_archive()


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, KeyError) as exc:
        # 不向共享日志系统输出可能包含源 URL、密钥或业务记录的异常正文。
        print(
            "BACKUP FAILED: "
            + type(exc).__name__
            + "; inspect private state and application readiness",
            file=sys.stderr,
        )
        sys.exit(1)
