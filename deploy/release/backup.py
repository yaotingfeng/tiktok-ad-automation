"""集群内完整备份与隔离恢复验证；正式数据库/Redis 永不作为恢复目标。

备份直接写入集群备份盘（PVC BACKUP_PVC，挂载于发布辅助 Pod 的 /backup），恢复验证也在
同一 Pod 内的临时 PostgreSQL/Redis 中完成：备份文件不离开新加坡集群，避免跨境回传
（数据库数 GB、跨境约 0.5 MiB/s）拉长停写时间。发布端只保存清单、校验和与恢复证明。

备份盘只保留最近一份完整备份 /backup/latest；新备份写入 .pending-* 目录，验证通过后
才替换旧备份。没有异地副本（用户 2026-10-16 决定），集群/可用区级故障依赖云数据库自身备份。
"""

import hashlib
import json
import os
import shlex
import tarfile
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

from common import PROJECT, digest, require, write

PROBE = Path(__file__).with_name("probe.py")
ARCHIVE_CHECK = Path(__file__).with_name("archive_check.py")
BACKUP_PVC = PROJECT + "-backup"
BACKUP_ROOT = "/backup"
# 恢复验证实例只监听辅助 Pod 内回环地址，端口与生产默认端口错开。
VERIFY_PG_PORT = 5433
VERIFY_REDIS_PORT = 6380
VERIFY_ENV = {
    "DATABASE_URL": f"postgresql+psycopg://postgres@127.0.0.1:{VERIFY_PG_PORT}/tkada_verify",
    "REDIS_URL": f"redis://127.0.0.1:{VERIFY_REDIS_PORT}/0",
}
# 辅助 Pod 的应用容器带着正式运行配置；恢复验证前覆盖连接并断言实际生效值，
# 覆盖失败时立即中止，绝不对正式数据库执行恢复、指纹或候选迁移。
VERIFY_GUARD = f"""import os
os.environ.update({VERIFY_ENV!r})
from app.core.config import settings
assert [(h["host"], h["port"]) for h in settings.DATABASE_URL.hosts()] == [("127.0.0.1", {VERIFY_PG_PORT})], "verify DATABASE_URL guard"
assert settings.REDIS_URL == {VERIFY_ENV["REDIS_URL"]!r}, "verify REDIS_URL guard"
"""


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


class PodExec:
    """在发布辅助 Pod 的指定容器内执行命令。

    容器约定：app（旧版应用镜像）、postgres（pg_dump 客户端）、verify-pg / verify-redis
    （恢复验证实例）、candidate（候选版本镜像，仅候选版本与旧版不同时存在）。各容器共享
    Pod 网络与 /backup 挂载；集成测试用同网络命名空间、同卷的 Docker 容器替代本类。
    """

    def __init__(self, cluster, pod):
        self.cluster, self.pod = cluster, pod

    def __call__(self, container, *args, data=None):
        stdin = ["-i"] if data is not None else []
        return self.cluster.k(
            "exec", *stdin, self.pod, "-c", container, "--", *args, data=data
        )


def sh(pod, container, script, *args, data=None):
    # 路径经位置参数传入，不拼进脚本文本，避免被 shell 解释。
    return pod(container, "sh", "-c", script, "sh", *args, data=data)


# 后台任务：脚本经 stdin 写入容器私有临时目录，setsid 脱离 exec 会话运行，结束时原子写退出码。
_DETACH = """umask 077
d=$(mktemp -d /tmp/tkada-job.XXXXXX)
cat > "$d/run.sh"
setsid sh -c 'd=$1; shift; sh "$d/run.sh" "$@" > "$d/log" 2>&1; echo $? > "$d/rc.tmp"; mv "$d/rc.tmp" "$d/rc"' sh "$d" "$@" </dev/null >/dev/null 2>&1 &
echo "$d"
"""
_POLL = 'if [ -e "$1/rc" ]; then cat "$1/rc"; else echo running; fi'
DETACHED_POLL_SECONDS = 3
DETACHED_TIMEOUT_SECONDS = 3600
# 轮询连接本身也可能断，连续失败超过此次数（约 2 分钟）才判定失败。
DETACHED_POLL_FAILURES = 40


def detached(pod, container, script, *args):
    """在容器内后台运行数分钟级脚本，再用短连接轮询退出码。

    发布端（公司 Jenkins，国内）到新加坡集群 API 的长时间 exec 会话会间歇断开，断开时远端
    进程随会话被终止：PG 导出只写出 toc.dat 就失败，造成停写后发布中止。脚本首行须删除自身，
    以免含连接凭据的脚本留在容器内。
    """
    # pod 返回原始字节（与 remote_digest 等调用一致）。
    job = pod(container, "sh", "-c", _DETACH, "sh", *args, data=script.encode())
    job = job.decode().strip().splitlines()[-1]
    require(job.startswith("/tmp/tkada-job."), "Detached job did not start")
    deadline = time.monotonic() + DETACHED_TIMEOUT_SECONDS
    failures = 0
    while True:
        time.sleep(DETACHED_POLL_SECONDS)
        try:
            status = pod(container, "sh", "-c", _POLL, "sh", job).decode().strip()
        except RuntimeError:
            failures += 1
            require(
                failures <= DETACHED_POLL_FAILURES, "Detached job status unreachable"
            )
            continue
        failures = 0
        if status != "running":
            break
        require(time.monotonic() < deadline, "Detached job timed out")
    log = pod(
        container, "sh", "-c", 'tail -c 4000 "$1/log"; rm -rf "$1"', "sh", job
    ).decode(errors="replace")
    require(status == "0", f"Detached job failed ({status}): {log[-2000:]}")
    return log


# PostgreSQL 用目录格式导出，备份里唯一的目录项；其余都是普通文件。
PG_DUMP = "postgres.dir"
# 单连接导出。源库经 PolarDB 读写分离代理（rwlb）访问：并行导出的子连接须导入主连接快照、
# 并以 NOWAIT 申请表锁，被代理分到其他节点或遇到排他锁请求即报错，只留下 toc.dat。
# 2026-10-18 发布多次因此在停写后失败；同库单连接实测 292 秒（并行 101–187 秒）。
PG_DUMP_JOBS = 1
# 目录的校验和：按文件名字节序逐个 sha256sum，再对整段输出取 sha256（与 archive_check.py 一致）。
TREE_DIGEST = 'cd "$1" && LC_ALL=C sha256sum -- * | sha256sum'


def tree_digest(path):
    """本机计算与 TREE_DIGEST 相同的目录校验和；文件则直接取 sha256。"""
    path = Path(path)
    if not path.is_dir():
        return digest(path)
    lines = "".join(
        f"{digest(item)}  {item.name}\n"
        for item in sorted(path.iterdir(), key=lambda p: p.name.encode())
        if item.is_file()
    )
    return hashlib.sha256(lines.encode()).hexdigest()


def remote_digest(pod, path, directory=False):
    if directory:
        return sh(pod, "app", TREE_DIGEST, path).decode().split()[0]
    return pod("app", "sha256sum", path).decode().split()[0]


def upload(pod, local, remote):
    sh(pod, "app", 'umask 077 && cat > "$1"', remote, data=Path(local).read_bytes())
    require(
        remote_digest(pod, remote) == digest(local), "Backup upload checksum mismatch"
    )


def guarded(pod, container, body, *args, data=None):
    return pod(container, "python", "-c", VERIFY_GUARD + body, *args, data=data)


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


def check_space(pod, database_bytes):
    """停写前确认备份盘余量，并清理上次失败遗留的未完成目录（发布锁保证无并发写入）。"""
    output = sh(
        pod,
        "app",
        'cd "$1" && rm -rf .pending-* .previous && df -B1 --output=avail . | tail -1'
        " && { if [ -e latest ]; then du -sb latest | cut -f1; else echo 0; fi; }",
        BACKUP_ROOT,
    ).split()
    available, last = int(output[0]), int(output[1])
    # 新备份验证完成、旧备份删除前两份并存，再留一份余量（按上一份完整备份的总大小）；
    # 首次没有上一份时按数据库大小估算（lz4 压缩实测约 3.5:1，按 1/4 留足）。
    required = max(3 * last, database_bytes // 4, 2 * 2**30)
    require(
        available >= required,
        f"Backup disk {BACKUP_PVC} has {available >> 20} MiB free, needs"
        f" {required >> 20} MiB; expand it in deploy/k8s/tk-ada.yaml before releasing",
    )
    return {"available": available, "last_dump": last, "required": required}


def snapshot(release):
    """在同一停写窗口内把全部备份写入备份盘的未完成目录，并生成清单。"""
    folder, pod = release.folder, release.pod
    target = release.state["backup_dir"]
    sh(pod, "app", 'umask 077 && mkdir "$1"', target)
    # pg_dump 凭据经 stdin 传给容器，不进入进程参数或控制台。
    exports = "\n".join(
        "export " + key + "=" + shlex.quote(value)
        for key, value in pg_environment(release.values).items()
    )
    detached(
        pod,
        "postgres",
        'rm -f "$0"\nset -e\n'
        + exports
        # 目录格式导出（ad_object 等数个 GB 级大表），lz4 压缩；单连接单快照，只读。
        + f"\npg_dump -Fd --jobs={PG_DUMP_JOBS} --compress=lz4"
        + " --no-owner --no-acl --schema=public -f "
        + shlex.quote(target + "/" + PG_DUMP)
        + "\n",
    )
    for filename, directory, excludes in [
        ("runtime-app.tar.gz", "/app", ["--exclude=.venv", "--exclude=__pycache__"]),
        # Beat 盘上的 .incident-* 是故障现场人工留存的快照（不完整、未经恢复验证），
        # 不属于 Beat 运行状态，不随每次发布重复打包。
        ("beat-data.tar.gz", "/var/run/celery", ["--exclude=.incident-*"]),
    ]:
        pod(
            "app",
            "tar",
            "-czf",
            target + "/" + filename,
            *excludes,
            "-C",
            directory,
            ".",
        )
    write(folder / "source-fingerprint.json", release.probe("fingerprint"))
    # probe 的附加参数按 JSON 透传，这里是备份盘上的导出文件路径。
    exported = release.probe("redis", target + "/redis.json")
    counted = pod(
        "app",
        "python",
        "-c",
        "import json,sys; print(len(json.load(open(sys.argv[1]))))",
        target + "/redis.json",
    )
    require(int(counted) == exported["records"], "Redis export record count mismatch")
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
    for name in ("source-fingerprint.json", "project.tar.gz", "config.tar.gz"):
        upload(pod, folder / name, target + "/" + name)
    files = (
        PG_DUMP,
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
            "files": {
                name: remote_digest(pod, target + "/" + name, directory=name == PG_DUMP)
                for name in files
            },
        },
    )
    upload(pod, folder / "manifest.json", target + "/manifest.json")


def verify_archives(pod, folder, directory):
    """备份盘上的清单须与发布端记录一致，再在集群内逐个核对校验和与归档结构。"""
    require(
        remote_digest(pod, directory + "/manifest.json")
        == digest(folder / "manifest.json"),
        "Backup manifest checksum mismatch",
    )
    result = json.loads(
        pod("app", "python", "-", directory, data=ARCHIVE_CHECK.read_bytes())
    )
    require(result["ok"], "Backup verification failed: " + str(result["error"]))


RESTORE_SCRIPT = f"""rm -f "$0"
set -e
pg_restore --list "$1" | grep -v ' SCHEMA - public ' > /tmp/restore.toc
# 多进程并行恢复数据与索引（单事务与并行互斥）；中途失败即退出，临时库随 Pod 删除。
pg_restore --use-list=/tmp/restore.toc --exit-on-error --jobs=4 \\
  --no-owner --no-acl -h 127.0.0.1 -p {VERIFY_PG_PORT} -U postgres -d tkada_verify "$1"
"""


def restore_verify(pod, folder, directory, candidate_sha=None, candidate="candidate"):
    """在辅助 Pod 内的临时实例上完整恢复并验证；candidate 为执行候选迁移的容器名。"""
    verify_archives(pod, folder, directory)
    for _ in range(120):
        try:
            pod(
                "verify-pg",
                "pg_isready",
                "-h",
                "127.0.0.1",
                "-p",
                str(VERIFY_PG_PORT),
                "-U",
                "postgres",
            )
            break
        except RuntimeError:
            time.sleep(1)
    else:
        raise RuntimeError("Isolated PostgreSQL failed to become ready")
    pod(
        "verify-pg",
        "psql",
        "-h",
        "127.0.0.1",
        "-p",
        str(VERIFY_PG_PORT),
        "-U",
        "postgres",
        "-d",
        "tkada_verify",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        'CREATE EXTENSION pg_trgm; CREATE EXTENSION "uuid-ossp";',
    )
    detached(pod, "verify-pg", RESTORE_SCRIPT, directory + "/" + PG_DUMP)
    fingerprint = json.loads(guarded(pod, "app", PROBE.read_text(), "fingerprint"))
    require(
        fingerprint == json.loads((folder / "source-fingerprint.json").read_text()),
        "Restored PostgreSQL facts/credentials differ",
    )
    # Redis DUMP/RESTORE 保留键内容及绝对过期时间，允许跨版本重新编码；验证每个有效键，不只比键数。
    redis_result = json.loads(
        sh(
            pod,
            "app",
            'exec python -c "$1" < "$2"',
            VERIFY_GUARD + REDIS_RESTORE_CODE,
            directory + "/redis.json",
        )
    )
    if candidate_sha:
        guarded(
            pod,
            candidate,
            "from alembic.config import main\nmain(argv=['upgrade', 'head'])",
        )
        # 恢复结果已与源库逐表核对；迁移后只需确认凭据仍可解密。
        after = json.loads(guarded(pod, candidate, PROBE.read_text(), "credentials"))
        require(
            after["credentials_decrypted"] == fingerprint["credentials_decrypted"],
            "Migration lost readable credentials",
        )
    write(
        folder / "restore-verified.json",
        {
            "manifest_sha256": digest(folder / "manifest.json"),
            "postgres_matches": True,
            "credentials_decrypted": fingerprint["credentials_decrypted"],
            "redis": redis_result,
            "candidate_sha": candidate_sha,
            "verified_at": int(time.time()),
        },
    )
    upload(pod, folder / "restore-verified.json", directory + "/restore-verified.json")


ROTATE_SCRIPT = """set -e
cd "$1"
rm -rf .previous
if [ -e latest ]; then mv latest .previous; fi
mv "$2" latest
sync
rm -rf .previous
"""


def publish(pod, folder, directory):
    """恢复验证通过后，以同盘目录改名替换为唯一保留的 /backup/latest。"""
    proof = json.loads((folder / "restore-verified.json").read_text())
    require(
        proof.get("postgres_matches") is True
        and "redis" in proof
        and proof.get("manifest_sha256") == digest(folder / "manifest.json"),
        "Successful restore verification required",
    )
    require(
        remote_digest(pod, directory + "/restore-verified.json")
        == digest(folder / "restore-verified.json"),
        "Restore proof on backup disk differs",
    )
    sh(pod, "app", ROTATE_SCRIPT, BACKUP_ROOT, directory)
    latest = BACKUP_ROOT + "/latest"
    # 同一文件系统内改名不重写内容；核对清单确认替换的是本次已验证的备份。
    require(
        remote_digest(pod, latest + "/manifest.json")
        == digest(folder / "manifest.json"),
        "Published cluster backup differs",
    )
    manifest = json.loads((folder / "manifest.json").read_text())
    write(
        folder / "cluster-backup.json",
        {
            "pvc": BACKUP_PVC,
            "path": latest,
            "manifest_sha256": digest(folder / "manifest.json"),
            "old_sha": manifest.get("old_sha"),
            "retained": 1,
            "encrypted": False,
            "verified_at": int(time.time()),
        },
    )
    return latest


def cleanup_completed(folder):
    """成功验收后删除本批发布端的私密配置副本；失败现场必须保留。"""
    folder = Path(folder)
    if not (folder / "cluster-backup.json").exists():
        return
    for name in (
        "cluster-before.json",
        "runtime.env",
        "mcp-client.json",
        "config.tar.gz",
        "project.tar.gz",
    ):
        (folder / name).unlink(missing_ok=True)
