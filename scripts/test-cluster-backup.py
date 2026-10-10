"""集群备份权限、隔离和保留行为；真实恢复由集群验收覆盖。"""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy/release"))
sys.path.insert(0, str(ROOT / "deploy/backup-cluster"))
from common import CONTEXT, PROJECT, digest, write
from controller import ClusterBackup, prune, restore_pod

spec = importlib.util.spec_from_file_location(
    "render_backup", ROOT / "deploy/backup-cluster/render.py"
)
render = importlib.util.module_from_spec(spec)
spec.loader.exec_module(render)
IMAGE = (
    "sg-acr-prod-registry-vpc.ap-southeast-1.cr.aliyuncs.com/test/tiktok-ad-automation@sha256:"
    + "a" * 64
)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_restore_environment_has_no_production_urls_or_cloud_keys(self):
        pod = restore_pod("restore", IMAGE)
        app = pod["spec"]["containers"][0]
        values = {e["name"]: e.get("value") for e in app["env"]}
        self.assertIn("127.0.0.1", values["DATABASE_URL"])
        self.assertIn("127.0.0.1", values["REDIS_URL"])
        self.assertFalse(any("S3" in name or "TIKTOK" in name for name in values))
        secret = next(e for e in app["env"] if e["name"] == "CONNECTION_ENCRYPTION_KEY")
        self.assertNotIn("value", secret)
        self.assertEqual(
            secret["valueFrom"]["secretKeyRef"]["key"], "CONNECTION_ENCRYPTION_KEY"
        )
        self.assertFalse(pod["spec"]["automountServiceAccountToken"])
        self.assertNotIn("volumeMounts", app)
        self.assertNotIn("nodeName", pod["spec"])
        self.assertNotIn("volumes", pod["spec"])

    def test_cron_defaults_suspended_and_cloud_disk_survives_pvc_deletion(self):
        objects = render.resources(IMAGE)["items"]
        cron = next(o for o in objects if o["kind"] == "CronJob")
        self.assertTrue(cron["spec"]["suspend"])
        self.assertEqual(cron["spec"]["concurrencyPolicy"], "Forbid")
        self.assertEqual(cron["spec"]["timeZone"], "Asia/Shanghai")
        self.assertEqual(
            next(o for o in objects if o["kind"] == "StorageClass")["reclaimPolicy"],
            "Retain",
        )

    def test_restore_policy_denies_all_ingress_and_egress(self):
        policy = next(
            o for o in render.resources(IMAGE)["items"] if o["kind"] == "NetworkPolicy"
        )
        self.assertEqual(policy["spec"]["egress"], [])
        self.assertEqual(policy["spec"]["ingress"], [])
        self.assertEqual(
            policy["spec"]["podSelector"]["matchLabels"]["role"], "backup-restore"
        )

    def test_role_cannot_read_other_projects_secrets_or_write_database_jobs(self):
        rules = next(
            o
            for o in render.resources(IMAGE)["items"]
            if o["kind"] == "Role" and o["metadata"]["namespace"] == "test"
        )["rules"]
        secret_rule = next(r for r in rules if "secrets" in r["resources"])
        self.assertEqual(secret_rule["verbs"], ["get"])
        self.assertEqual(
            set(secret_rule["resourceNames"]),
            {PROJECT + "-" + n for n in ("runtime", "mcp-registration", "registry")},
        )
        self.assertFalse(any("jobs" in r["resources"] for r in rules))
        scale = next(r for r in rules if "deployments/scale" in r["resources"])
        self.assertEqual(len(scale["resourceNames"]), 9)

    def test_removed_retired_deployment_is_safe_only_without_orphan_pods(self):
        with patch.dict(os.environ, K8S_CONTEXT=CONTEXT, K8S_NAMESPACE="test"):
            instance = ClusterBackup(self.root / "backup-fixture")
        retired = {
            "namespace": "prod",
            "items": [
                {
                    "kind": "Deployment",
                    "metadata": {"name": PROJECT + "-api", "uid": "old"},
                }
            ],
        }
        with (
            patch(
                "controller.Path.read_text",
                return_value=__import__("json").dumps(retired),
            ),
            patch("controller.run", side_effect=[b"", b'{"items":[]}']),
        ):
            instance.check_retired_source()
        with (
            patch(
                "controller.Path.read_text",
                return_value=__import__("json").dumps(retired),
            ),
            patch(
                "controller.run",
                side_effect=[b"", b'{"items":[{"status":{"phase":"Running"}}]}'],
            ),
            self.assertRaises(RuntimeError),
        ):
            instance.check_retired_source()

    def test_mutable_or_foreign_image_rejected(self):
        for image in [
            IMAGE.replace("@sha256:", ":latest-"),
            IMAGE.replace("tiktok-ad-automation", "other"),
        ]:
            with self.assertRaises(ValueError):
                render.resources(image)

    def fixture(self, name, timestamp):
        path = self.root / name
        path.mkdir()
        (path / "backup.tar.age").write_bytes(b"ciphertext")
        write(path / "encrypted.json", {"sha256": digest(path / "backup.tar.age")})
        write(path / "completed.json", {"completed_at": timestamp})
        return path

    def test_retention_keeps_latest_and_never_deletes_failed_or_foreign_folders(self):
        old = self.fixture("backup-old", 1)
        recent = self.fixture("backup-recent", 2)
        latest = self.fixture("backup-latest", 3)
        failed = self.root / "backup-failed"
        failed.mkdir()
        foreign = self.root / "unrelated"
        foreign.mkdir()
        prune(self.root, 2)
        self.assertFalse(old.exists())
        self.assertTrue(all(p.exists() for p in (recent, latest, failed, foreign)))

    def test_corrupt_kept_archive_stops_retention(self):
        old = self.fixture("backup-old", 1)
        corrupt = self.fixture("backup-corrupt", 2)
        (corrupt / "backup.tar.age").write_bytes(b"damage")
        with self.assertRaises(RuntimeError):
            prune(self.root, 2)
        self.assertTrue(old.exists())

    def test_backup_failure_recovers_services_but_still_fails_job(self):
        with patch.dict(os.environ, K8S_CONTEXT=CONTEXT, K8S_NAMESPACE="test"):
            instance = ClusterBackup(self.root / "backup-fixture")
        instance.state = {"phase": "frozen"}
        with (
            patch.object(instance, "begin"),
            patch.object(instance, "prepare_tools"),
            patch.object(instance, "pause"),
            patch("controller.snapshot", side_effect=RuntimeError("backup failure")),
            patch.object(instance, "cleanup_restore") as cleanup,
            patch.object(instance, "recover") as recover,
            patch.object(instance, "complete_archive") as complete,
        ):
            with self.assertRaises(RuntimeError):
                instance.execute_backup()
            cleanup.assert_called_once()
            recover.assert_called_once()
            complete.assert_not_called()

    def test_resumption_failure_is_not_blindly_retried(self):
        with patch.dict(os.environ, K8S_CONTEXT=CONTEXT, K8S_NAMESPACE="test"):
            instance = ClusterBackup(self.root / "backup-fixture")
        instance.state = {"phase": "resuming"}
        with (
            patch.object(instance, "begin"),
            patch.object(
                instance, "prepare_tools", side_effect=RuntimeError("failure")
            ),
            patch.object(instance, "recover") as recover,
        ):
            with self.assertRaises(RuntimeError):
                instance.execute_backup()
            recover.assert_not_called()


if __name__ == "__main__":
    unittest.main()
