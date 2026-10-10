"""使用纯合成数据验证真实 PG18/Redis8 恢复与 Celery 暂停；不连接生产。"""

import base64
import json
import os
import secrets
import sys
import tarfile
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy/release"))
from backup import PROBE, restore_verify
from common import PUBLIC, digest, require, run, write


def main():
    token = secrets.token_hex(5)
    name = "tkada-backup-test-" + token
    sha = secrets.token_hex(20)
    image = PUBLIC + sha
    source = os.environ.get(
        "RELEASE_TEST_IMAGE", "tiktok-ad-automation:deploy-check-amd64"
    )
    run(["docker", "tag", source, image])
    net = run(["docker", "network", "create", "--internal", name]).decode().strip()
    containers = []
    # 用户目录在 Colima 的共享范围内；不从本机私有环境文件加载配置。
    with tempfile.TemporaryDirectory(
        prefix="release-test-", dir=ROOT / ".runtime"
    ) as temp:
        folder = Path(temp)
        pg, red = name + "-pg", name + "-redis"
        values = {
            "PROJECT_NAME": "release synthetic test",
            "DATABASE_URL": f"postgresql+psycopg://postgres:fixture@{pg}:5432/tkada",
            "REDIS_URL": f"redis://{red}:6379/15",
            "FIRST_SUPERUSER": "admin",
            "FIRST_SUPERUSER_PASSWORD": "fixture-test-only",
            "SECRET_KEY": "fixture-signing-key-not-a-production-secret",
            "FASTAPI_ENV": "development",
            "MATERIAL_INGEST_ENABLED": "false",
            "MATERIAL_CLEANUP_ENABLED": "false",
            "ADS_SYNC_ENABLED": "false",
            "CONNECTION_ENCRYPTION_KEY": base64.urlsafe_b64encode(
                secrets.token_bytes(32)
            ).decode(),
        }
        write(
            folder / "source.env",
            "".join(f"{k}={v}\n" for k, v in values.items()).encode(),
        )
        app = [
            "docker",
            "run",
            "--rm",
            "-i",
            "--network",
            name,
            "--env-file",
            str(folder / "source.env"),
            image,
        ]
        try:
            containers.append(
                run(
                    [
                        "docker",
                        "run",
                        "-d",
                        "--name",
                        pg,
                        "--network",
                        name,
                        "-e",
                        "POSTGRES_PASSWORD=fixture",
                        "-e",
                        "POSTGRES_DB=tkada",
                        "postgres:18",
                    ]
                )
                .decode()
                .strip()
            )
            containers.append(
                run(
                    ["docker", "run", "-d", "--name", red, "--network", name, "redis:8"]
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
            run(
                [
                    "docker",
                    "exec",
                    pg,
                    "psql",
                    "-U",
                    "postgres",
                    "-d",
                    "tkada",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-c",
                    "CREATE EXTENSION pg_trgm; CREATE EXTENSION \"uuid-ossp\"; CREATE TABLE fixture(id int PRIMARY KEY, data text); INSERT INTO fixture VALUES(1,'验证实际恢复'),(2,'durable');",
                ]
            )
            run(app + ["alembic", "upgrade", "head"])
            credentials_fixture = """from uuid import uuid4
from sqlalchemy import create_engine,text
from app.core.config import settings
from app.core.credentials import encrypt_credentials
with create_engine(str(settings.DATABASE_URL)).begin() as c:
 c.execute(text('CREATE TABLE fixture_credentials(tenant_id uuid,credential_ciphertext text)'))
 tenant=uuid4()
 c.execute(text('INSERT INTO fixture_credentials VALUES(:tenant,:cipher)'),{'tenant':tenant,'cipher':encrypt_credentials(tenant_id=tenant,value={'token':'synthetic-only'})})
"""
            run(app + ["python", "-c", credentials_fixture])
            run(
                [
                    "docker",
                    "exec",
                    pg,
                    "pg_dump",
                    "-U",
                    "postgres",
                    "-d",
                    "tkada",
                    "-Fc",
                    "--schema=public",
                    "--no-owner",
                    "--no-acl",
                    "-f",
                    "/tmp/fixture.dump",
                ]
            )
            run(
                [
                    "docker",
                    "cp",
                    pg + ":/tmp/fixture.dump",
                    str(folder / "postgres.dump"),
                ]
            )
            write(
                folder / "source-fingerprint.json",
                json.loads(
                    run(app + ["python", "-", "fingerprint"], data=PROBE.read_bytes())
                ),
            )
            code = """import os,redis
r=redis.Redis.from_url(os.environ['REDIS_URL'])
r.set('string',b'\\x00secret');r.rpush('queue',b'one',b'two');r.hset('hash',mapping={b'k':b'v'});r.sadd('set',b'a',b'b');r.zadd('zset',{b'a':1.2});r.set('expiry','value',px=600000)
"""
            run(app + ["python", "-c", code])
            records = json.loads(
                run(app + ["python", "-", "redis"], data=PROBE.read_bytes())
            )
            require(len(records) == 6, "Expected all Redis fixture types")
            write(folder / "redis.json", records)
            for archive_name, entry in [
                ("project", "README.md"),
                ("runtime-app", "backend/app/frontend/index.html"),
                ("beat-data", "celerybeat-schedule"),
                ("config", "runtime.env"),
            ]:
                target = folder / (archive_name + "-fixture")
                target.write_bytes(b"fixture")
                with tarfile.open(
                    folder / (archive_name + ".tar.gz"), "w:gz"
                ) as archive:
                    archive.add(target, arcname=entry)
            files = [
                "postgres.dump",
                "redis.json",
                "source-fingerprint.json",
                "project.tar.gz",
                "runtime-app.tar.gz",
                "beat-data.tar.gz",
                "config.tar.gz",
            ]
            write(
                folder / "manifest.json",
                {"files": {f: digest(folder / f) for f in files}},
            )
            restore_verify(folder, values, sha, sha)
            result = json.loads((folder / "restore-verified.json").read_text())
            require(
                result["credentials_decrypted"] == 1,
                "Encrypted credential did not survive restore",
            )
            require(
                result["redis"]["verified_keys"] == 6,
                "Redis restore did not verify every fixture key",
            )
            print(
                "PASS: isolated PostgreSQL restore, encrypted credential, candidate migration, six Redis key types/TTL",
                flush=True,
            )
            # 损坏备份必须在启动恢复容器前被拒绝。
            with (folder / "postgres.dump").open("ab") as stream:
                stream.write(b"corrupt")
            try:
                restore_verify(folder, values, sha, sha)
            except RuntimeError as exc:
                require("checksum" in str(exc), "Unexpected corruption error")
            else:
                raise RuntimeError("Corrupt backup accepted")
            print("PASS: corrupt backup rejected", flush=True)
            celery_test(app, folder, name, image, containers)
        finally:
            for cid in reversed(containers):
                run(["docker", "rm", "-f", "-v", cid])
            run(["docker", "network", "rm", net])
            run(["docker", "image", "rm", image])


def celery_test(app, folder, network, image, containers):
    # 使用真实 app Celery 配置、Redis 和 prefork；合成任务不调用平台。
    fixture = """import os,time,redis
from app.jobs.celery_app import celery_app
@celery_app.task(name='fixture.slow')
def slow(i):
 r=redis.Redis.from_url(os.environ['REDIS_URL']);r.set('fixture.started',i);time.sleep(4);r.rpush('fixture.done',i)
"""
    worker = network + "-worker"
    cid = (
        run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                worker,
                "--hostname",
                worker,
                "--network",
                network,
                "--env-file",
                str(folder / "source.env"),
                image,
                "sleep",
                "600",
            ]
        )
        .decode()
        .strip()
    )
    containers.append(cid)
    write(folder / "fixture.py", fixture.encode())
    run(["docker", "cp", str(folder / "fixture.py"), cid + ":/app/backend/fixture.py"])
    run(
        [
            "docker",
            "exec",
            "-d",
            cid,
            "celery",
            "-A",
            "fixture:celery_app",
            "worker",
            "-Q",
            "builds",
            "--pool",
            "prefork",
            "--concurrency",
            "1",
            "--hostname",
            "fixture@%h",
            "--without-gossip",
            "--without-mingle",
        ]
    )
    expected = ["fixture@" + worker]
    for _ in range(45):
        try:
            run(
                app + ["python", "-", "inspect", json.dumps(expected)],
                data=PROBE.read_bytes(),
            )
            break
        except RuntimeError:
            time.sleep(1)
    run(
        app
        + [
            "python",
            "-c",
            "from app.jobs.celery_app import celery_app; [celery_app.send_task('fixture.slow',args=[i],queue='builds') for i in range(5)]",
        ]
    )
    run(app + ["python", "-", "cancel", json.dumps(expected)], data=PROBE.read_bytes())
    from release import drained

    for _ in range(30):
        result = json.loads(
            run(
                app + ["python", "-", "inspect", json.dumps(expected)],
                data=PROBE.read_bytes(),
            )
        )
        if drained(result, expected):
            break
        time.sleep(1)
    else:
        raise RuntimeError("Celery did not settle after cancel_consumer")
    code = "import os,json,redis;r=redis.Redis.from_url(os.environ['REDIS_URL']);print(json.dumps({'queued':r.llen('builds'),'done':r.llen('fixture.done')}))"
    counts = json.loads(run(app + ["python", "-c", code]))
    require(
        counts["queued"] > 0 and counts["queued"] + counts["done"] == 5,
        "Queued or reserved task was lost",
    )
    run(
        app
        + [
            "python",
            "-c",
            "from app.jobs.celery_app import celery_app; celery_app.control.add_consumer('builds',destination="
            + repr(expected)
            + ",reply=True)",
        ]
    )
    for _ in range(40):
        counts = json.loads(run(app + ["python", "-c", code]))
        if counts["done"] == 5:
            break
        time.sleep(1)
    require(counts["done"] == 5 and counts["queued"] == 0, "Tasks failed to resume")
    print(
        "PASS: real Celery cancel/drain preserves queued tasks and resumes all five",
        flush=True,
    )


if __name__ == "__main__":
    (ROOT / ".runtime").mkdir(exist_ok=True)
    main()
