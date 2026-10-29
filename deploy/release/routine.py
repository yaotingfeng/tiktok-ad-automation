"""普通发布只切换程序；数据合同变化仍进入完整备份/迁移流程。"""

import json
import re
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor

from backup import BACKUP_ROOT, remote_digest, sh, upload
from common import ROOT, digest, require, run, write


def needs_full_backup(old_sha, sha, *, force=False):
    if force:
        return True
    paths = (
        run(["git", "-C", str(ROOT), "diff", "--name-only", old_sha, sha])
        .decode()
        .splitlines()
    )
    # 这些变更不能仅 set image；还需迁移、队列合同或正式部署清单应用。
    sensitive = re.compile(
        r"^(backend/app/(alembic/|jobs/|models\.py|core/)"
        r"|backend/app/modules/.*/jobs(?:/|\.py$)"
        r"|backend/.*(?:models?|schema|contracts?)(?:/|\.py$)"
        r"|deploy/k8s/|config/|\.env\.example$|backend/Dockerfile$"
        r"|uv\.lock$|pyproject\.toml$|backend/pyproject\.toml$)"
    )
    return any(sensitive.search(path) for path in paths if not path.endswith(".md"))


def recent_backup(pod, folder, current_heads, now=None):
    """普通发布前核验备份盘中的证据，不以 Jenkins 的历史成功状态代替。"""
    latest = BACKUP_ROOT + "/latest"
    manifest_bytes = pod("app", "cat", latest + "/manifest.json", idempotent=True)
    proof = json.loads(
        pod("app", "cat", latest + "/restore-verified.json", idempotent=True)
    )
    manifest = json.loads(manifest_bytes)
    write(folder / "recent-backup-manifest.json", manifest_bytes)
    require(
        proof.get("manifest_sha256") == digest(folder / "recent-backup-manifest.json"),
        "Recent backup proof mismatch",
    )
    now = time.time() if now is None else now
    captured = manifest.get("captured_at", 0)
    require(
        0 <= now - captured <= 259200,
        "Verified database backup is older than 72 hours; run backup-online first",
    )
    require(
        captured <= proof.get("verified_at", 0) <= now,
        "Invalid backup verification time",
    )
    frozen = (
        proof.get("postgres_matches") is True
        and manifest.get("consistency", "frozen") == "frozen"
    )
    online = (
        manifest.get("consistency") == proof.get("consistency") == "online-postgres"
        and proof.get("postgres_restored") is True
        and bool(proof.get("restored_table_rows"))
    )
    require(frozen or online, "Recent database restore was not verified")
    require(
        {
            "postgres.dir",
            "source-fingerprint.json",
            "config.tar.gz",
            "project.tar.gz",
            "runtime-app.tar.gz",
        }
        <= manifest["files"].keys(),
        "Recent backup is incomplete",
    )

    # 只扫描备份数据的校验和，不再次恢复全库；文件损坏仍阻断普通发布。
    def verify_file(item):
        name, checksum = item
        require(
            re.fullmatch(r"[a-zA-Z0-9_.-]+", name) and name not in (".", ".."),
            "Invalid backup member",
        )
        require(
            remote_digest(pod, latest + "/" + name, directory=name == "postgres.dir")
            == checksum,
            "Recent backup checksum mismatch",
        )

    # 各文件核验互不依赖；保留完整字节校验，压缩跨区域 kubectl 的串行等待。
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(verify_file, manifest["files"].items()))
    source = json.loads(
        pod("app", "cat", latest + "/source-fingerprint.json", idempotent=True)
    )
    require(
        source["migration_heads"] == current_heads,
        "Recent backup schema differs from production",
    )
    write(
        folder / "recent-backup.json",
        {
            "manifest_sha256": proof["manifest_sha256"],
            "captured_at": captured,
            "verified_at": proof["verified_at"],
            "consistency": manifest.get("consistency", "frozen"),
        },
    )


def snapshot_release(release):
    """每次保留程序/配置/冻结 Redis；明确不包含同一时点的 PG 全备。"""
    folder, pod = release.folder, release.pod
    target = BACKUP_ROOT + "/.release-" + release.state["token"][:12]
    sh(pod, "app", 'umask 077; mkdir "$1"', target)
    pod(
        "app",
        "tar",
        "-czf",
        target + "/runtime-app.tar.gz",
        "--exclude=.venv",
        "--exclude=__pycache__",
        "-C",
        "/app",
        ".",
    )
    release.archive_project(folder / "project.tar.gz")
    with tarfile.open(folder / "config.tar.gz", "w:gz") as archive:
        for name in (
            "cluster-before.json",
            "runtime.env",
            "mcp-client.json",
            "state.json",
            "recent-backup.json",
        ):
            archive.add(folder / name, arcname=name)
    release.probe("redis", target + "/redis.json")
    for name in ("project.tar.gz", "config.tar.gz"):
        upload(pod, folder / name, target + "/" + name)
        require(
            remote_digest(pod, target + "/" + name) == digest(folder / name),
            "Release archive upload mismatch",
        )
    files = ("runtime-app.tar.gz", "project.tar.gz", "config.tar.gz", "redis.json")
    record = {
        "kind": "program-config-redis-only",
        "old_sha": release.state["old_sha"],
        "captured_at": int(time.time()),
        "database_backup": json.loads((folder / "recent-backup.json").read_text()),
        "files": {n: remote_digest(pod, target + "/" + n) for n in files},
    }
    write(folder / "release-archive.json", record)
    upload(pod, folder / "release-archive.json", target + "/manifest.json")
    # 仅轮换程序回退材料；绝不覆盖 /backup/latest 数据库备份。
    sh(
        pod,
        "app",
        'set -e; cd "$1"; rm -rf .previous-release; if [ -e latest-release ]; then mv latest-release .previous-release; fi; mv "$2" latest-release; rm -rf .previous-release',
        BACKUP_ROOT,
        target,
    )
