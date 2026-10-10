"""完整备份、隔离恢复及加密远端回读。正式数据库永不作为恢复目标。"""

import json
import os
import secrets
import shlex
import tarfile
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

from common import PUBLIC, digest, require, run, write

PROBE = Path(__file__).with_name("probe.py")


REDIS_RESTORE_CODE = (
    "probe = {'__name__': 'backup_probe'}\nexec("
    + repr(PROBE.read_text())
    + ", probe)\n"
    + """import base64,json,time,redis,sys
r=redis.Redis.from_url(__import__('os').environ['REDIS_URL'])
records=json.load(sys.stdin)
verified=0
reencoded=0
for item in records:
 key=base64.b64decode(item['key']); value=base64.b64decode(item['dump']); expiry=item['expires_at_ms']
 if expiry is not None and expiry<=int(time.time()*1000): continue
 r.restore(key,expiry or 0,value,absttl=expiry is not None)
 if expiry is None or expiry>int(time.time()*1000)+100:
  assert r.type(key).decode()==item['type'], 'Redis type mismatch'
  assert probe['redis_logical'](r,key,item['type'])==item['logical'], 'Redis content mismatch'
  if expiry is None: assert r.pttl(key)==-1
  if expiry: assert abs(r.pexpiretime(key)-expiry)<2
  reencoded+=r.dump(key)!=value
  verified+=1
print(json.dumps({'verified_keys':verified,'reencoded_keys':reencoded}))
"""
)


def pg_environment(values):
    url = urlsplit(values["DATABASE_URL"])
    require(url.path == "/tkada", "Source PostgreSQL database must be tkada")
    return {
        "PGHOST": url.hostname,
        "PGPORT": str(url.port or 5432),
        "PGUSER": unquote(url.username or ""),
        "PGPASSWORD": unquote(url.password or ""),
        "PGDATABASE": "tkada",
    }


def remote_file(cluster, pod, container, remote, local):
    expected = (
        cluster.k("exec", pod, "-c", container, "--", "sha256sum", remote)
        .decode()
        .split()[0]
    )
    env = dict(os.environ, KUBECTL_REMOTE_COMMAND_WEBSOCKETS="false")
    run(
        cluster.base
        + ["cp", "--retries=8", "-c", container, pod + ":" + remote, str(local)],
        env=env,
    )
    require(digest(local) == expected, "Backup transfer checksum mismatch")
    local.chmod(0o600)


def snapshot(release):
    folder, cluster, pod = release.folder, release.cluster, release.state["helper"]
    # pg_dump 凭据经 stdin 传给容器，不进入本机进程参数或控制台。
    exports = "\n".join(
        "export " + key + "=" + shlex.quote(value)
        for key, value in pg_environment(release.values).items()
    )
    cluster.k(
        "exec",
        "-i",
        pod,
        "-c",
        "postgres",
        "--",
        "sh",
        "-s",
        data=(
            exports
            + "\npg_dump -Fc --no-owner --no-acl --schema=public -f /tmp/postgres.dump\n"
        ).encode(),
    )
    remote_file(
        cluster, pod, "postgres", "/tmp/postgres.dump", folder / "postgres.dump"
    )
    for filename, directory, excludes in [
        ("runtime-app.tar.gz", "/app", ["--exclude=.venv", "--exclude=__pycache__"]),
        ("beat-data.tar.gz", "/var/run/celery", []),
    ]:
        cluster.k(
            "exec",
            pod,
            "-c",
            "app",
            "--",
            "tar",
            "-czf",
            "/tmp/" + filename,
            *excludes,
            "-C",
            directory,
            ".",
        )
        remote_file(cluster, pod, "app", "/tmp/" + filename, folder / filename)
    write(folder / "source-fingerprint.json", release.probe("fingerprint"))
    write(folder / "redis.json", release.probe("redis"))
    release.archive_project(folder / "project.tar.gz")
    # 清单包含运行配置、注册材料、原副本与旧部署、共享入口证书引用。
    with tarfile.open(folder / "config.tar.gz", "w:gz") as archive:
        for name in (
            "cluster-before.json",
            "runtime.env",
            "mcp-client.json",
            "state.json",
        ):
            archive.add(folder / name, arcname=name)
        retired = os.environ.get("RETIRED_SOURCE_INVENTORY")
        if retired:
            archive.add(retired, arcname="retired-source.json")
    files = (
        "postgres.dump",
        "runtime-app.tar.gz",
        "beat-data.tar.gz",
        "project.tar.gz",
        "config.tar.gz",
        "source-fingerprint.json",
        "redis.json",
    )
    write(
        folder / "manifest.json",
        {
            "old_sha": release.state["old_sha"],
            "captured_at": int(time.time()),
            "files": {name: digest(folder / name) for name in files},
        },
    )


def verify_archives(folder):
    manifest = json.loads((folder / "manifest.json").read_text())
    for name, checksum in manifest["files"].items():
        require(
            Path(name).name == name and digest(folder / name) == checksum,
            "Backup checksum mismatch",
        )
    for name in ("runtime-app", "project", "beat-data", "config"):
        with tarfile.open(folder / (name + ".tar.gz")) as archive:
            members = archive.getmembers()
            require(bool(members), "Empty archive: " + name)
            require(
                all(
                    not m.name.startswith("/") and ".." not in Path(m.name).parts
                    for m in members
                ),
                "Unsafe archive member",
            )
            if name == "runtime-app":
                require(
                    any(
                        m.name.endswith("backend/app/frontend/index.html")
                        for m in members
                    ),
                    "Frontend artifact missing",
                )
            # 解包普通文件验证压缩内容可读，避免仅检查目录。
            for member in members:
                if member.isfile():
                    with archive.extractfile(member) as stream:
                        while stream.read(1024 * 1024):
                            pass


def restore_verify(folder, values, old_sha, candidate_sha=None):
    verify_archives(folder)
    name = "tkada-restore-" + secrets.token_hex(6)
    net, pg, redis_name = name + "-net", name + "-pg", name + "-redis"
    password = secrets.token_urlsafe(32)
    write(
        folder / "verify-pg.env",
        f"POSTGRES_PASSWORD={password}\nPOSTGRES_DB=tkada_verify\n".encode(),
    )
    env = dict(
        values,
        DATABASE_URL=f"postgresql+psycopg://postgres:{password}@{pg}:5432/tkada_verify",
        REDIS_URL=f"redis://{redis_name}:6379/0",
    )
    write(
        folder / "verify-app.env",
        "".join(f"{k}={v}\n" for k, v in env.items()).encode(),
    )
    ids = []
    network_id = None
    try:
        network_id = (
            run(["docker", "network", "create", "--internal", net]).decode().strip()
        )
        ids.append(
            run(
                [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    pg,
                    "--network",
                    net,
                    "--env-file",
                    str(folder / "verify-pg.env"),
                    "postgres:18",
                ]
            )
            .decode()
            .strip()
        )
        ids.append(
            run(
                [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    redis_name,
                    "--network",
                    net,
                    "redis:8",
                    "redis-server",
                    "--save",
                    "",
                    "--appendonly",
                    "no",
                ]
            )
            .decode()
            .strip()
        )
        for _ in range(60):
            try:
                run(
                    [
                        "docker",
                        "exec",
                        pg,
                        "pg_isready",
                        "-h",
                        "127.0.0.1",
                        "-U",
                        "postgres",
                    ]
                )
                break
            except RuntimeError:
                time.sleep(1)
        else:
            raise RuntimeError("Isolated PostgreSQL failed to become ready")
        run(
            [
                "docker",
                "exec",
                pg,
                "psql",
                "-U",
                "postgres",
                "-d",
                "tkada_verify",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                'CREATE EXTENSION pg_trgm; CREATE EXTENSION "uuid-ossp";',
            ]
        )
        run(["docker", "cp", str(folder / "postgres.dump"), pg + ":/tmp/backup.dump"])
        toc = run(
            ["docker", "exec", pg, "pg_restore", "--list", "/tmp/backup.dump"]
        ).decode()
        write(
            folder / "restore.toc",
            (
                "\n".join(
                    line for line in toc.splitlines() if " SCHEMA - public " not in line
                )
                + "\n"
            ).encode(),
        )
        run(["docker", "cp", str(folder / "restore.toc"), pg + ":/tmp/restore.toc"])
        run(
            [
                "docker",
                "exec",
                pg,
                "pg_restore",
                "--use-list=/tmp/restore.toc",
                "--exit-on-error",
                "--single-transaction",
                "--no-owner",
                "--no-acl",
                "-U",
                "postgres",
                "-d",
                "tkada_verify",
                "/tmp/backup.dump",
            ]
        )
        app = [
            "docker",
            "run",
            "--rm",
            "-i",
            "--network",
            net,
            "--env-file",
            str(folder / "verify-app.env"),
        ]
        fingerprint = json.loads(
            run(
                app + [PUBLIC + old_sha, "python", "-", "fingerprint"],
                data=PROBE.read_bytes(),
            )
        )
        require(
            fingerprint == json.loads((folder / "source-fingerprint.json").read_text()),
            "Restored PostgreSQL facts/credentials differ",
        )
        # Redis DUMP/RESTORE 保留键内容及绝对过期时间，允许跨版本重新编码；验证每个有效键，不只比键数。
        redis_result = json.loads(
            run(
                app + [PUBLIC + old_sha, "python", "-c", REDIS_RESTORE_CODE],
                data=(folder / "redis.json").read_bytes(),
            )
        )
        if candidate_sha:
            run(app + [PUBLIC + candidate_sha, "alembic", "upgrade", "head"])
            after = json.loads(
                run(
                    app + [PUBLIC + candidate_sha, "python", "-", "fingerprint"],
                    data=PROBE.read_bytes(),
                )
            )
            require(
                after["credentials_decrypted"] == fingerprint["credentials_decrypted"],
                "Migration lost readable credentials",
            )
        write(
            folder / "restore-verified.json",
            {
                "postgres_matches": True,
                "credentials_decrypted": fingerprint["credentials_decrypted"],
                "redis": redis_result,
                "candidate_sha": candidate_sha,
                "verified_at": int(time.time()),
            },
        )
    finally:
        for cid in reversed(ids):
            run(["docker", "rm", "-f", "-v", cid])
        if network_id:
            run(["docker", "network", "rm", network_id])
        (folder / "verify-pg.env").unlink(missing_ok=True)
        (folder / "verify-app.env").unlink(missing_ok=True)


def upload(folder):
    bucket, prefix, recipient = (
        os.environ[name]
        for name in ("BACKUP_BUCKET", "BACKUP_PREFIX", "BACKUP_AGE_RECIPIENT")
    )
    require(
        bucket and prefix and recipient.startswith("age1"),
        "Explicit backup destination and age recipient required",
    )
    key = prefix.rstrip("/") + "/" + folder.name + ".tar.age"
    bundle = folder / "backup.tar"
    manifest = json.loads((folder / "manifest.json").read_text())
    with tarfile.open(bundle, "w") as archive:
        for name in [*manifest["files"], "manifest.json", "restore-verified.json"]:
            archive.add(folder / name, arcname=name)
    encrypted = folder / "backup.tar.age"
    run(["age", "-r", recipient, "-o", str(encrypted), str(bundle)])
    bundle.unlink()
    aws = ["aws"]
    if os.environ.get("BACKUP_ENDPOINT_URL"):
        aws += ["--endpoint-url", os.environ["BACKUP_ENDPOINT_URL"]]
    run(
        aws
        + [
            "s3api",
            "put-object",
            "--bucket",
            bucket,
            "--key",
            key,
            "--body",
            str(encrypted),
        ]
    )
    copy = folder / "remote-readback.age"
    run(aws + ["s3api", "get-object", "--bucket", bucket, "--key", key, str(copy)])
    require(
        digest(copy) == digest(encrypted), "Remote backup readback checksum mismatch"
    )
    copy.unlink()
    write(
        folder / "upload-verified.json",
        {"bucket": bucket, "key": key, "sha256": digest(encrypted), "encrypted": True},
    )
