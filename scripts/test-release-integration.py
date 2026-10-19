"""使用纯合成数据验证真实 PG18/Redis7到8 恢复与 Celery 暂停；不连接生产。

发布辅助 Pod 用同一网络命名空间、同一备份卷的 Docker 容器模拟：app 与 verify-pg/
verify-redis 共享回环地址，备份写在卷上的 /backup，执行路径与集群内完全相同。
"""

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
from backup import (
    BACKUP_ROOT,
    PG_DUMP,
    PG_DUMP_JOBS,
    PROBE,
    REDIS_RESTORE_CODE,
    VERIFY_PG_PORT,
    VERIFY_REDIS_PORT,
    check_space,
    publish,
    remote_digest,
    restore_verify,
    sh,
    tree_digest,
    upload,
)
from common import PUBLIC, mirrored, require, write
from common import run as common_run

sys.path.insert(0, str(ROOT / "deploy/ci"))
from resource_labels import with_owner


def run(args, **kwargs):
    # 恢复测试创建的容器、网络、卷与外层构建同属一个标签，中止后可从宿主回收。
    return common_run(with_owner(args, os.environ.get("TKADA_CI_OWNER", "")), **kwargs)


class DockerPod:
    """与 backup.PodExec 相同的调用约定：按容器名执行命令。"""

    def __init__(self, containers):
        self.containers = containers

    def __call__(self, container, *args, data=None):
        stdin = ["-i"] if data is not None else []
        return run(
            ["docker", "exec", *stdin, self.containers[container], *args], data=data
        )


def start_pod(name, network, image, env_file, containers):
    """app 容器挂备份卷并接入源库网络；验证实例共用其网络命名空间，只监听回环地址。"""
    volume = name + "-backup"
    run(["docker", "volume", "create", volume])
    app = (
        run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                name + "-app",
                "--network",
                network,
                "--env-file",
                str(env_file),
                "-v",
                volume + ":" + BACKUP_ROOT,
                image,
                "sleep",
                "600",
            ]
        )
        .decode()
        .strip()
    )
    containers.append(app)
    shared = ["--network", "container:" + app]
    verify_pg = (
        run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                name + "-verify-pg",
                *shared,
                "-e",
                "POSTGRES_HOST_AUTH_METHOD=trust",
                "-e",
                "POSTGRES_DB=tkada_verify",
                "-v",
                volume + ":" + BACKUP_ROOT + ":ro",
                mirrored("postgres:18"),
                "-c",
                "listen_addresses=127.0.0.1",
                "-p",
                str(VERIFY_PG_PORT),
            ]
        )
        .decode()
        .strip()
    )
    containers.append(verify_pg)
    verify_redis = (
        run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                name + "-verify-redis",
                *shared,
                mirrored("redis:8"),
                "redis-server",
                "--bind",
                "127.0.0.1",
                "--port",
                str(VERIFY_REDIS_PORT),
                "--save",
                "",
                "--appendonly",
                "no",
            ]
        )
        .decode()
        .strip()
    )
    containers.append(verify_redis)
    return DockerPod(
        {"app": app, "verify-pg": verify_pg, "verify-redis": verify_redis}
    ), volume


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
            "AUTOMATED_RULES_ENABLED": "false",
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
                        mirrored("postgres:18"),
                    ]
                )
                .decode()
                .strip()
            )
            containers.append(
                run(
                    [
                        "docker",
                        "run",
                        "-d",
                        "--name",
                        red,
                        "--network",
                        name,
                        mirrored("redis:7.0"),
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
                    # 与发布端相同：目录格式、并行、lz4。
                    "-Fd",
                    f"--jobs={PG_DUMP_JOBS}",
                    "--compress=lz4",
                    "--schema=public",
                    "--no-owner",
                    "--no-acl",
                    "-f",
                    "/tmp/fixture.dir",
                ]
            )
            run(["docker", "cp", pg + ":/tmp/fixture.dir", str(folder / PG_DUMP)])
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
            # 与发布端一致：探针把导出写入容器文件，标准输出只回条数；
            # --rm 容器不保留文件，故在同一容器内导出后立即读取。
            output = run(
                app
                + [
                    "sh",
                    "-c",
                    "python - redis '\"/tmp/redis.json\"' && cat /tmp/redis.json",
                ],
                data=PROBE.read_bytes(),
            )
            summary, _, body = output.partition(b"\n")
            exported, records = json.loads(summary), json.loads(body)
            require(
                exported == {"records": 6} and len(records) == 6,
                "Expected all Redis fixture types",
            )
            require(
                all(
                    int.from_bytes(base64.b64decode(r["dump"])[-10:-8], "little") == 10
                    for r in records
                ),
                "Expected older Redis DUMP version",
            )
            damaged = json.loads(json.dumps(records))
            damaged[0]["logical"] = ["wrong fixture content"]
            try:
                run(
                    app
                    + [
                        "python",
                        "-c",
                        "import os; os.environ['REDIS_URL'] = os.environ['REDIS_URL'].rsplit('/', 1)[0] + '/14'\n"
                        + REDIS_RESTORE_CODE,
                    ],
                    data=json.dumps(damaged).encode(),
                )
            except RuntimeError:
                pass
            else:
                raise RuntimeError("Wrong Redis logical content accepted")
            write(folder / "redis.json", records)
            pod, _volume = start_pod(
                name, name, image, folder / "source.env", containers
            )
            space = check_space(pod, 1)
            require(space["last_dump"] == 0, "Fresh backup disk has no previous dump")
            directory = BACKUP_ROOT + "/.pending-fixture"
            sh(pod, "app", 'umask 077 && mkdir "$1"', directory)
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
                PG_DUMP,
                "redis.json",
                "source-fingerprint.json",
                "project.tar.gz",
                "runtime-app.tar.gz",
                "beat-data.tar.gz",
                "config.tar.gz",
            ]
            for f in files:
                if (folder / f).is_dir():
                    sh(pod, "app", 'umask 077 && mkdir "$1"', directory + "/" + f)
                    for item in (folder / f).iterdir():
                        upload(pod, item, directory + "/" + f + "/" + item.name)
                else:
                    upload(pod, folder / f, directory + "/" + f)
            write(
                folder / "manifest.json",
                {"files": {f: tree_digest(folder / f) for f in files}},
            )
            upload(pod, folder / "manifest.json", directory + "/manifest.json")
            # 备份卷之外的源库仍可从 app 容器访问：恢复只能落在回环临时实例上。
            restore_verify(pod, folder, directory, sha, candidate="app")
            latest = publish(pod, folder, directory)
            require(
                remote_digest(pod, latest + "/" + PG_DUMP, directory=True)
                == tree_digest(folder / PG_DUMP),
                "Published backup content differs",
            )
            source_rows = run(
                [
                    "docker",
                    "exec",
                    pg,
                    "psql",
                    "-U",
                    "postgres",
                    "-d",
                    "tkada",
                    "-Atc",
                    "SELECT count(*) FROM fixture",
                ]
            )
            require(
                source_rows.strip() == b"2", "Source database changed during verify"
            )
            result = json.loads((folder / "restore-verified.json").read_text())
            require(
                result["credentials_decrypted"] == 1,
                "Encrypted credential did not survive restore",
            )
            require(
                result["redis"]["verified_keys"] == 6,
                "Redis restore did not verify every fixture key",
            )
            require(
                result["redis"]["reencoded_keys"] == 6,
                "Expected cross-version Redis serialization changes",
            )
            print(
                "PASS: isolated PostgreSQL restore, encrypted credential, candidate migration, six Redis key types/TTL",
                flush=True,
            )
            # 下次发布需要为新旧两份并存留足余量。
            space = check_space(pod, 1)
            require(
                space["last_dump"] > 0,
                "Space check must size against the retained dump",
            )
            # 在线备份允许源库继续写；恢复必须保留 dump 的快照，不能伪称与当前源库逐行一致。
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
                    "-c",
                    "INSERT INTO fixture VALUES(3,'after snapshot')",
                ]
            )
            write(
                folder / "source-fingerprint.json",
                json.loads(
                    run(app + ["python", "-", "schema"], data=PROBE.read_bytes())
                ),
            )
            write(folder / "redis.json", [])
            for name_part in ("source-fingerprint.json", "redis.json"):
                upload(pod, folder / name_part, latest + "/" + name_part)
            manifest = json.loads((folder / "manifest.json").read_text())
            manifest["consistency"] = "online-postgres"
            for name_part in ("source-fingerprint.json", "redis.json"):
                manifest["files"][name_part] = tree_digest(folder / name_part)
            write(folder / "manifest.json", manifest)
            upload(pod, folder / "manifest.json", latest + "/manifest.json")
            pod(
                "verify-pg",
                "dropdb",
                "-h",
                "127.0.0.1",
                "-p",
                str(VERIFY_PG_PORT),
                "-U",
                "postgres",
                "tkada_verify",
            )
            pod(
                "verify-pg",
                "createdb",
                "-h",
                "127.0.0.1",
                "-p",
                str(VERIFY_PG_PORT),
                "-U",
                "postgres",
                "tkada_verify",
            )
            restore_verify(pod, folder, latest, online=True)
            proof = json.loads((folder / "restore-verified.json").read_text())
            require(
                proof["postgres_restored"] and not proof["postgres_matches"],
                "Online proof must not claim frozen source equality",
            )
            require(
                proof["restored_table_rows"]["fixture"] == 2
                and proof["credentials_decrypted"] == 1,
                "Online restore must preserve original snapshot and credentials",
            )
            source_rows = run(
                [
                    "docker",
                    "exec",
                    pg,
                    "psql",
                    "-U",
                    "postgres",
                    "-d",
                    "tkada",
                    "-Atc",
                    "SELECT count(*) FROM fixture",
                ]
            )
            require(
                source_rows.strip() == b"3",
                "Online verification must leave live writes intact",
            )
            print(
                "PASS: online database snapshot restored while source keeps newer writes; no cross-store consistency claim",
                flush=True,
            )
            # 备份盘上的损坏备份必须在恢复前被拒绝。
            sh(
                pod,
                "app",
                'printf corrupt >> "$1"',
                latest + "/" + PG_DUMP + "/toc.dat",
            )
            try:
                restore_verify(pod, folder, latest, sha, candidate="app")
            except RuntimeError as exc:
                require("checksum" in str(exc), "Unexpected corruption error")
            else:
                raise RuntimeError("Corrupt backup accepted")
            print("PASS: published to backup disk; corrupt backup rejected", flush=True)
            celery_test(app, folder, name, image, containers)
        finally:
            for cid in reversed(containers):
                run(["docker", "rm", "-f", "-v", cid])
            run(["docker", "volume", "rm", "-f", name + "-backup"])
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
