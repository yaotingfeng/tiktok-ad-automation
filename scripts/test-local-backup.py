"""本地备份替换的行为验证：失败保留旧副本，成功仅保留一份。"""

import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy/release"))
from backup import cleanup_completed, publish_local, verify_bundle
from common import digest, write


class LocalBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "state"
        self.source.mkdir()
        self.target = self.root / "backups"
        env = patch.dict(os.environ, LOCAL_BACKUP_DIR=str(self.target))
        env.start()
        self.addCleanup(env.stop)
        self.fixture(b"first")

    def fixture(self, payload):
        files = []
        for name in ("runtime-app", "project", "beat-data", "config"):
            path = self.source / (name + ".tar.gz")
            with tarfile.open(path, "w:gz") as archive:
                info = tarfile.TarInfo(
                    "backend/app/frontend/index.html"
                    if name == "runtime-app"
                    else "data"
                )
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
            files.append(path.name)
        for name in ("postgres.dump", "redis.json", "source-fingerprint.json"):
            write(self.source / name, payload)
            files.append(name)
        write(
            self.source / "manifest.json",
            {"old_sha": "a" * 40, "files": {n: digest(self.source / n) for n in files}},
        )
        write(
            self.source / "restore-verified.json",
            {
                "manifest_sha256": digest(self.source / "manifest.json"),
                "postgres_matches": True,
                "redis": {"verified_keys": 1},
            },
        )

    def test_success_replaces_one_archive_and_preserves_unrelated_files(self):
        target = publish_local(self.source)
        before = digest(target)
        write(self.target / "notes.json", {"keep": True})
        self.fixture(b"second")
        publish_local(self.source)
        self.assertNotEqual(digest(target), before)
        self.assertEqual(list(self.target.glob("*.tar")), [target])
        self.assertTrue((self.target / "notes.json").exists())
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o700)

    def test_readback_failure_keeps_previous_and_cleans_pending(self):
        target = publish_local(self.source)
        before = target.read_bytes()
        self.fixture(b"second")
        with (
            patch("backup.verify_bundle", side_effect=RuntimeError("corrupt readback")),
            self.assertRaises(RuntimeError),
        ):
            publish_local(self.source)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(list(self.target.glob(".pending-*")), [])

    def test_unverified_or_corrupt_source_cannot_replace_previous(self):
        target = publish_local(self.source)
        before = digest(target)
        write(self.source / "restore-verified.json", {"postgres_matches": False})
        with self.assertRaises(RuntimeError):
            publish_local(self.source)
        self.fixture(b"second")
        (self.source / "postgres.dump").write_bytes(b"corruption")
        with self.assertRaisesRegex(RuntimeError, "checksum"):
            publish_local(self.source)
        self.assertEqual(digest(target), before)

    def test_actual_bundle_corruption_rejected(self):
        target = publish_local(self.source)
        manifest = json.loads((self.source / "manifest.json").read_text())
        checksums = {
            n: digest(self.source / n)
            for n in [*manifest["files"], "manifest.json", "restore-verified.json"]
        }
        checksums["postgres.dump"] = "0" * 64
        with self.assertRaisesRegex(RuntimeError, "checksum"):
            verify_bundle(target, checksums)

    def test_symlink_and_same_folder_rejected(self):
        with (
            patch.dict(os.environ, LOCAL_BACKUP_DIR=str(self.source)),
            self.assertRaises(RuntimeError),
        ):
            publish_local(self.source)
        self.target.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            publish_local(self.source)
        self.target.unlink()
        self.target.mkdir()
        (self.target / "latest.tar").symlink_to(self.source / "postgres.dump")
        with self.assertRaises(RuntimeError):
            publish_local(self.source)

    def test_completed_cleanup_keeps_evidence_and_single_restorable_archive(self):
        target = publish_local(self.source)
        write(self.source / "runtime.env", b"private")
        write(self.source / "state.json", {"phase": "complete"})
        cleanup_completed(self.source)
        self.assertFalse((self.source / "runtime.env").exists())
        self.assertFalse((self.source / "postgres.dump").exists())
        self.assertTrue((self.source / "state.json").exists())
        with tarfile.open(target) as archive:
            self.assertIn("postgres.dump", archive.getnames())
            self.assertIn("config.tar.gz", archive.getnames())


class LocalEntryTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "local_release", ROOT / "deploy/local.py"
        )
        self.local = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.local)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()

        def git(*args):
            return subprocess.run(
                ["git", *args], cwd=self.repo, check=True, capture_output=True
            )

        git("init", "-b", "main")
        git("config", "user.email", "fixture@example.invalid")
        git("config", "user.name", "Fixture")
        git("remote", "add", "company", self.local.COMPANY)
        (self.repo / "source.txt").write_text("committed")
        git("add", "source.txt")
        git("commit", "-m", "fixture")
        original = self.local.git
        self.patches = [
            patch.object(self.local, "ROOT", self.repo),
            patch.object(
                self.local,
                "git",
                side_effect=lambda *args, **kwargs: original(
                    *args, cwd=kwargs.get("cwd", self.repo)
                ),
            ),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_checkout_excludes_dirty_and_private_files_without_switching_branch(self):
        (self.repo / "source.txt").write_text("user changes")
        (self.repo / ".env").write_text("fixture-secret")
        folder = self.root / "run"
        folder.mkdir()
        checkout, sha = self.local.prepare_checkout(folder)
        self.assertEqual((checkout / "source.txt").read_text(), "committed")
        self.assertFalse((checkout / ".env").exists())
        self.assertEqual((self.repo / "source.txt").read_text(), "user changes")
        self.assertEqual(self.local.git("branch", "--show-current"), "main")
        self.assertEqual(self.local.git("rev-parse", "HEAD", cwd=checkout), sha)
        self.assertEqual(self.local.git("status", "--porcelain", cwd=checkout), "")

    def test_wrong_company_remote_rejected_before_checkout(self):
        self.local.git("remote", "set-url", "company", "https://example.invalid/wrong")
        with self.assertRaisesRegex(RuntimeError, "company"):
            self.local.prepare_checkout(self.root / "run")
        self.assertFalse((self.root / "run/checkout").exists())

    def test_check_basis_reuses_only_records_covering_current_production(self):
        def commit(text):
            (self.repo / "source.txt").write_text(text)
            subprocess.run(["git", "commit", "-qam", text], cwd=self.repo, check=True)
            return self.local.git("rev-parse", "HEAD")

        deployed = self.local.git("rev-parse", "HEAD")
        checked = commit("checked")
        head = commit("head")
        state = self.root / "state"
        (state / "checks").mkdir(parents=True)

        def record(sha, **fields):
            (state / "checks" / (sha + ".json")).write_text(
                json.dumps({"sha": sha, **fields})
            )

        basis = lambda online: self.local.check_basis(  # noqa: E731
            state, head, online, repo=self.repo
        )
        self.assertIsNone(basis(deployed))
        # 只覆盖到其他线上版本的增量记录不能复用。
        record(checked, full=False, base="0" * 40)
        self.assertIsNone(basis(deployed))
        record(checked, full=False, base=deployed)
        self.assertEqual(basis(deployed)["sha"], checked)
        # 线上版本变化后，增量记录失效；全量记录始终可作为起点，且优先最近的祖先。
        self.assertIsNone(basis(checked))
        record(head, full=True)
        self.assertEqual(basis(checked)["sha"], head)


if __name__ == "__main__":
    unittest.main()
