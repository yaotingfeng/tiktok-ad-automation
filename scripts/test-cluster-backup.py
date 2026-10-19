"""集群备份盘轮换与归档校验的行为验证；Pod 执行替换为本机临时目录。"""

import io
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy/release"))
import backup
from backup import (
    BACKUP_ROOT,
    PG_DUMP,
    cleanup_completed,
    publish,
    remote_digest,
    tree_digest,
    upload,
    verify_archives,
)
from common import digest, write
from routine import needs_full_backup, recent_backup


class LocalPod:
    """把 /backup 映射到临时目录并在本机执行，等价于辅助 Pod 内的同一命令。"""

    def __init__(self, root):
        self.root = str(root)

    def __call__(self, _container, *args, data=None):
        args = [a.replace(BACKUP_ROOT, self.root) for a in args]
        if args[0] == "python":
            args[0] = sys.executable
        result = subprocess.run(args, input=data, capture_output=True, check=False)
        if result.returncode:
            raise RuntimeError(f"{args[0]} failed ({result.returncode})")
        return result.stdout


class ClusterBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.disk = root / "disk"
        self.disk.mkdir()
        self.state = root / "state"
        self.state.mkdir()
        self.pod = LocalPod(self.disk)

    def fixture(self, payload, name=".pending-one"):
        """在备份盘上生成一份完整备份，并在发布端保留对应清单与恢复证明。"""
        remote = self.disk / name
        remote.mkdir()
        files = []
        for archive_name in ("runtime-app", "project", "beat-data", "config"):
            path = remote / (archive_name + ".tar.gz")
            with tarfile.open(path, "w:gz") as archive:
                info = tarfile.TarInfo(
                    "backend/app/frontend/index.html"
                    if archive_name == "runtime-app"
                    else "data"
                )
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
            files.append(path.name)
        # 与 pg_dump -Fd 相同：一个 toc.dat 加按表分开的数据文件。
        (remote / PG_DUMP).mkdir()
        (remote / PG_DUMP / "toc.dat").write_bytes(b"toc-" + payload)
        (remote / PG_DUMP / "3001.dat.lz4").write_bytes(payload)
        files.append(PG_DUMP)
        for file_name in ("redis.json", "source-fingerprint.json"):
            (remote / file_name).write_bytes(payload)
            files.append(file_name)
        write(
            self.state / "manifest.json",
            {"old_sha": "a" * 40, "files": {n: tree_digest(remote / n) for n in files}},
        )
        upload(
            self.pod,
            self.state / "manifest.json",
            BACKUP_ROOT + "/" + name + "/manifest.json",
        )
        write(
            self.state / "restore-verified.json",
            {
                "manifest_sha256": digest(self.state / "manifest.json"),
                "postgres_matches": True,
                "redis": {"verified_keys": 1},
            },
        )
        upload(
            self.pod,
            self.state / "restore-verified.json",
            BACKUP_ROOT + "/" + name + "/restore-verified.json",
        )
        return BACKUP_ROOT + "/" + name

    def test_valid_backup_passes_and_corruption_is_rejected(self):
        directory = self.fixture(b"first")
        verify_archives(self.pod, self.state, directory)
        with (self.disk / ".pending-one" / PG_DUMP / "3001.dat.lz4").open(
            "ab"
        ) as stream:
            stream.write(b"x")
        with self.assertRaisesRegex(RuntimeError, "checksum"):
            verify_archives(self.pod, self.state, directory)

    def test_routine_backup_age_schema_and_corruption_gates(self):
        self.fixture(b"first")
        pending = self.disk / ".pending-one"
        write(pending / "source-fingerprint.json", {"migration_heads": ["v1"]})
        manifest = json.loads((self.state / "manifest.json").read_text())
        manifest["captured_at"] = 100000
        manifest["files"]["source-fingerprint.json"] = digest(
            pending / "source-fingerprint.json"
        )
        write(self.state / "manifest.json", manifest)
        write(pending / "manifest.json", manifest)
        proof = {
            "manifest_sha256": digest(self.state / "manifest.json"),
            "postgres_matches": True,
            "redis": {},
            "verified_at": 100100,
        }
        write(self.state / "restore-verified.json", proof)
        write(pending / "restore-verified.json", proof)
        publish(self.pod, self.state, BACKUP_ROOT + "/.pending-one")
        recent_backup(self.pod, self.state, ["v1"], now=100200)
        with self.assertRaisesRegex(RuntimeError, "24 hours"):
            recent_backup(self.pod, self.state, ["v1"], now=200000)
        with self.assertRaisesRegex(RuntimeError, "schema"):
            recent_backup(self.pod, self.state, ["v2"], now=100200)
        (self.disk / "latest" / PG_DUMP / "3001.dat.lz4").write_bytes(b"damaged")
        with self.assertRaisesRegex(RuntimeError, "checksum"):
            recent_backup(self.pod, self.state, ["v1"], now=100200)

    def test_data_and_deployment_changes_force_full_release(self):
        for path in (
            "backend/app/alembic/versions/new.py",
            "backend/app/jobs/celery_app.py",
            "backend/app/modules/reporting/jobs.py",
            "backend/app/modules/ads/jobs/reconcile.py",
            "backend/app/modules/ads/models.py",
            "deploy/k8s/tk-ada.yaml",
            "uv.lock",
        ):
            with (
                self.subTest(path=path),
                patch("routine.run", return_value=path.encode()),
            ):
                self.assertTrue(needs_full_backup("a" * 40, "b" * 40))
        with patch(
            "routine.run",
            return_value=b"backend/app/modules/ads/read.py\nfrontend/src/page.tsx",
        ):
            self.assertFalse(needs_full_backup("a" * 40, "b" * 40))
            self.assertTrue(needs_full_backup("a" * 40, "b" * 40, force=True))

    def test_manifest_must_match_release_record(self):
        directory = self.fixture(b"first")
        write(self.state / "manifest.json", {"files": {}})
        with self.assertRaisesRegex(RuntimeError, "manifest"):
            verify_archives(self.pod, self.state, directory)

    def test_unsafe_member_and_missing_frontend_are_rejected(self):
        directory = self.fixture(b"first")
        path = self.disk / ".pending-one/runtime-app.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            info = tarfile.TarInfo("other.txt")
            archive.addfile(info, io.BytesIO(b""))
        manifest = json.loads((self.state / "manifest.json").read_text())
        manifest["files"]["runtime-app.tar.gz"] = digest(path)
        write(self.state / "manifest.json", manifest)
        upload(self.pod, self.state / "manifest.json", directory + "/manifest.json")
        with self.assertRaisesRegex(RuntimeError, "Frontend"):
            verify_archives(self.pod, self.state, directory)

    def test_publish_keeps_exactly_one_latest(self):
        publish(self.pod, self.state, self.fixture(b"first"))
        first = tree_digest(self.disk / "latest" / PG_DUMP)
        publish(self.pod, self.state, self.fixture(b"second", ".pending-two"))
        self.assertNotEqual(tree_digest(self.disk / "latest" / PG_DUMP), first)
        self.assertEqual(sorted(p.name for p in self.disk.iterdir()), ["latest"])
        record = json.loads((self.state / "cluster-backup.json").read_text())
        self.assertEqual(record["path"], BACKUP_ROOT + "/latest")
        self.assertEqual(record["retained"], 1)

    def test_unverified_backup_never_replaces_latest(self):
        publish(self.pod, self.state, self.fixture(b"first"))
        directory = self.fixture(b"second", ".pending-two")
        proof = json.loads((self.state / "restore-verified.json").read_text())
        proof["postgres_matches"] = False
        write(self.state / "restore-verified.json", proof)
        with self.assertRaises(RuntimeError):
            publish(self.pod, self.state, directory)
        self.assertEqual(
            (self.disk / "latest" / PG_DUMP / "3001.dat.lz4").read_bytes(), b"first"
        )

    def test_proof_on_disk_must_match_release_record(self):
        directory = self.fixture(b"first")
        (self.disk / ".pending-one/restore-verified.json").write_text("{}")
        with self.assertRaisesRegex(RuntimeError, "proof"):
            publish(self.pod, self.state, directory)
        self.assertFalse((self.disk / "latest").exists())

    def test_cleanup_only_after_published_and_removes_private_copies(self):
        for name in (
            "runtime.env",
            "mcp-client.json",
            "cluster-before.json",
            "config.tar.gz",
        ):
            write(self.state / name, b"secret")
        cleanup_completed(self.state)
        self.assertTrue((self.state / "runtime.env").exists())
        publish(self.pod, self.state, self.fixture(b"first"))
        cleanup_completed(self.state)
        for name in (
            "runtime.env",
            "mcp-client.json",
            "cluster-before.json",
            "config.tar.gz",
        ):
            self.assertFalse((self.state / name).exists())
        self.assertTrue((self.state / "manifest.json").exists())

    def test_restore_is_parallel_and_never_single_transaction(self):
        # pg_restore 的 --jobs 与 --single-transaction 互斥；恢复失败仍须立即退出。
        self.assertIn("--jobs=4", backup.RESTORE_SCRIPT)
        self.assertIn("--exit-on-error", backup.RESTORE_SCRIPT)
        self.assertNotIn("--single-transaction", backup.RESTORE_SCRIPT)

    def test_remote_tree_digest_matches_local_and_detects_missing_file(self):
        directory = self.fixture(b"first")
        local = self.disk / ".pending-one" / PG_DUMP
        self.assertEqual(
            remote_digest(self.pod, directory + "/" + PG_DUMP, True), tree_digest(local)
        )
        (local / "toc.dat").unlink()
        with self.assertRaisesRegex(RuntimeError, "checksum"):
            verify_archives(self.pod, self.state, directory)

    def test_verify_guard_pins_loopback_instances(self):
        self.assertIn("127.0.0.1", backup.VERIFY_ENV["DATABASE_URL"])
        self.assertIn(str(backup.VERIFY_PG_PORT), backup.VERIFY_GUARD)
        self.assertIn("verify REDIS_URL guard", backup.VERIFY_GUARD)


if __name__ == "__main__":
    unittest.main()
