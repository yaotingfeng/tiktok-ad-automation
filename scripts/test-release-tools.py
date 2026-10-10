"""发布门禁测试；Kubernetes/命令运输在边界替身，真实依赖恢复另见 integration。"""

import copy
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy/release"))
from common import CONTEXT, PROJECT, ROLES, VPC, Cluster, write
from release import Release, drained


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, K8S_CONTEXT=CONTEXT, K8S_NAMESPACE="test")
        self.env.start()
        self.addCleanup(self.env.stop)
        self.release = Release(Path(self.tmp.name) / "run")
        self.release.folder.mkdir()
        self.deployments = {
            PROJECT + "-" + role: {
                "metadata": {"uid": role, "labels": {"app": PROJECT}},
                "spec": {
                    "replicas": 1,
                    "template": {"spec": {"containers": [{"image": VPC + "a" * 40}]}},
                },
            }
            for role in ROLES
        }
        self.release.state = {
            "deployments": copy.deepcopy(self.deployments),
            "context": CONTEXT,
            "namespace": "test",
            "phase": "frozen",
            "token": "fixture",
            "lock_uid": "lock",
        }

    def test_prepare_pulls_production_images_as_amd64_on_any_host(self):
        from common import PUBLIC

        self.release.state["old_sha"] = "a" * 40
        with (
            patch("release.run", return_value=b"") as command,
            patch.object(
                self.release,
                "image_digest",
                side_effect=RuntimeError("stop after pulls"),
            ),
            self.assertRaisesRegex(RuntimeError, "stop after pulls"),
        ):
            self.release.prepare_tools("b" * 40)
        pulls = [call.args[0] for call in command.call_args_list]
        self.assertIn(
            ["docker", "pull", "--platform=linux/amd64", PUBLIC + "a" * 40], pulls
        )
        self.assertIn(
            ["docker", "pull", "--platform=linux/amd64", PUBLIC + "b" * 40], pulls
        )
        self.assertIn(["docker", "pull", "postgres:18"], pulls)

    def test_image_digest_uses_registry_index_reported_by_ack(self):
        index = "sha256:" + "1" * 64
        child = "sha256:" + "2" * 64
        descriptor = {
            "digest": index,
            "manifests": [
                {"digest": child, "platform": {"architecture": "amd64", "os": "linux"}}
            ],
        }
        from common import PUBLIC

        pulled = [PUBLIC.rsplit(":", 1)[0] + "@" + index]
        with patch(
            "release.run",
            side_effect=[json.dumps(descriptor).encode(), json.dumps(pulled).encode()],
        ):
            self.assertEqual(self.release.image_digest("a" * 40), index)

    def test_changed_registry_tag_cannot_validate_a_different_local_image(self):
        descriptor = {"digest": "sha256:" + "1" * 64}
        from common import PUBLIC

        pulled = [PUBLIC.rsplit(":", 1)[0] + "@sha256:" + "2" * 64]
        with (
            patch(
                "release.run",
                side_effect=[
                    json.dumps(descriptor).encode(),
                    json.dumps(pulled).encode(),
                ],
            ),
            self.assertRaisesRegex(RuntimeError, "Pulled image"),
        ):
            self.release.image_digest("a" * 40)

    def test_missing_worker_never_counts_as_drained(self):
        self.assertFalse(
            drained(
                {k: {} for k in ("active", "reserved", "scheduled", "active_queues")},
                ["one"],
            )
        )

    def test_queue_backlog_is_not_a_drain_requirement(self):
        reply = {
            k: {"one": []} for k in ("active", "reserved", "scheduled", "active_queues")
        }
        reply["queue_lengths"] = {"builds": 1000}
        self.assertTrue(drained(reply, ["one"]))
        reply["reserved"]["one"] = [{"id": "pending"}]
        self.assertFalse(drained(reply, ["one"]))

    def test_scheduled_task_and_live_consumer_block_freeze(self):
        for kind in ("active", "reserved", "scheduled", "active_queues"):
            reply = {
                k: {"one": []}
                for k in ("active", "reserved", "scheduled", "active_queues")
            }
            reply[kind]["one"] = [{}]
            self.assertFalse(drained(reply, ["one"]))

    def test_drain_requires_two_consecutive_idle_samples(self):
        idle = {
            k: {"one": []} for k in ("active", "reserved", "scheduled", "active_queues")
        }
        busy = copy.deepcopy(idle)
        busy["active"]["one"] = [{"id": "moving-from-reserved"}]
        with (
            patch.object(
                self.release, "probe", side_effect=[idle, busy, idle, idle]
            ) as probe,
            patch("release.time.sleep") as sleep,
        ):
            self.release.wait_drained(["one"])
            self.assertEqual(probe.call_count, 4)
            self.assertEqual(sleep.call_count, 3)

    def test_other_target_rejected_before_commands(self):
        with (
            patch.dict(os.environ, K8S_CONTEXT="wrong"),
            patch("common.run") as command,
        ):
            with self.assertRaises(RuntimeError):
                Cluster()
            command.assert_not_called()

    def test_foreign_lock_is_never_released(self):
        with (
            patch.object(
                self.release.cluster,
                "get",
                return_value={
                    "metadata": {"uid": "other"},
                    "data": {"token": "fixture"},
                },
            ),
            patch.object(self.release.cluster, "k") as command,
        ):
            with self.assertRaises(RuntimeError):
                self.release.unlock()
            command.assert_not_called()

    def test_resource_replacement_blocks_recovery(self):
        self.deployments[PROJECT + "-api"]["metadata"]["uid"] = "replaced"
        with (
            patch.object(
                self.release.cluster, "deployments", return_value=self.deployments
            ),
            self.assertRaises(RuntimeError),
        ):
            self.release.validate_deployments()

    def test_changed_template_blocks_old_resume(self):
        self.deployments[PROJECT + "-api"]["spec"]["template"]["spec"]["containers"][0][
            "image"
        ] = VPC + "b" * 40
        with (
            patch.object(
                self.release.cluster, "deployments", return_value=self.deployments
            ),
            self.assertRaises(RuntimeError),
        ):
            self.release.validate_deployments(original=True)

    def test_migration_started_never_automatically_recovers_old_code(self):
        self.release.state["phase"] = "migration-started"
        with (
            patch.object(self.release, "load"),
            patch.object(self.release, "resume") as resume,
        ):
            with self.assertRaises(RuntimeError):
                self.release.recover()
            resume.assert_not_called()

    def test_state_is_valid_json_and_private(self):
        path = self.release.folder / "state.json"
        write(path, {"phase": "frozen"})
        self.assertEqual(json.loads(path.read_text()), {"phase": "frozen"})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_drain_failure_never_stops_workers(self):
        self.release.timeout = 0
        pods = [
            {"metadata": {"name": role, "labels": {"role": role}}}
            for role in ROLES
            if role.startswith("worker-")
        ]
        with (
            patch.object(self.release, "assert_lock"),
            patch.object(self.release, "validate_deployments"),
            patch.object(self.release, "helper"),
            patch.object(self.release, "wait_pods"),
            patch.object(self.release, "probe", return_value={}),
            patch.object(self.release.cluster, "app_pods", return_value=pods),
            patch.object(self.release.cluster, "scale") as scale,
        ):
            with self.assertRaises(RuntimeError):
                self.release.pause()
            self.assertEqual(
                [c.args[0] for c in scale.call_args_list],
                [PROJECT + "-beat", PROJECT + "-api"],
            )
            self.assertEqual(self.release.state["phase"], "draining")

    def test_failed_backup_does_not_migrate_or_resume(self):
        with (
            patch.object(self.release, "begin"),
            patch.object(self.release, "prepare_tools"),
            patch.object(self.release, "check_remote"),
            patch.object(self.release, "pause"),
            patch("release.snapshot"),
            patch("release.restore_verify", side_effect=RuntimeError("bad backup")),
            patch("release.publish_local") as upload,
            patch("release.run") as command,
            patch.object(self.release, "resume") as resume,
        ):
            self.release.state["old_sha"] = "a" * 40
            with self.assertRaises(RuntimeError):
                self.release.execute("deploy", "b" * 40)
            upload.assert_not_called()
            command.assert_not_called()
            resume.assert_not_called()

    def test_obsolete_commit_does_not_pause(self):
        with (
            patch.object(self.release, "begin"),
            patch.object(self.release, "prepare_tools"),
            patch.object(
                self.release, "check_remote", side_effect=RuntimeError("newer main")
            ),
            patch.object(self.release, "pause") as pause,
        ):
            with self.assertRaises(RuntimeError):
                self.release.execute("deploy", "b" * 40)
            pause.assert_not_called()

    def test_unexpected_candidate_never_starts_services(self):
        with (
            patch.object(self.release, "assert_lock"),
            patch.object(
                self.release, "validate_deployments", return_value=self.deployments
            ),
            patch.object(self.release.cluster, "scale") as scale,
        ):
            with self.assertRaises(RuntimeError):
                self.release.resume("b" * 40)
            scale.assert_not_called()

    def test_failed_migration_keeps_zero_replicas_for_inspection(self):
        with (
            patch.object(self.release, "begin"),
            patch.object(self.release, "prepare_tools"),
            patch.object(self.release, "check_remote"),
            patch.object(self.release, "pause"),
            patch("release.snapshot"),
            patch("release.restore_verify"),
            patch("release.publish_local"),
            patch.object(self.release, "assert_lock"),
            patch.object(self.release, "config_unchanged"),
            patch.object(self.release.cluster, "app_pods", return_value=[]),
            patch("release.run", side_effect=RuntimeError("migration failed")),
            patch.object(self.release, "resume") as resume,
        ):
            self.release.state["old_sha"] = "a" * 40
            with self.assertRaises(RuntimeError):
                self.release.execute("deploy", "b" * 40)
            self.assertEqual(self.release.state["phase"], "migration-started")
            resume.assert_not_called()

    def test_apply_holds_workloads_until_orchestrator_resumes(self):
        spec = importlib.util.spec_from_file_location(
            "operations", ROOT / "deploy/k8s/operations.py"
        )
        operations = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(operations)
        with patch.dict(
            os.environ,
            INGRESS_HOST="tkada.shortclipflix.com",
            RELEASE_HOLD_WORKLOADS="1",
        ):
            obj = operations.Deployment()
            obj.image, obj.release, obj.job_name, obj.digest = (
                VPC + "b" * 40,
                "b" * 40,
                "migration",
                "digest",
            )
            obj.values, obj.registration, obj.existing_job = (
                dict.fromkeys(operations.FLAGS, "false"),
                b"{}",
                False,
            )
            with (
                patch.object(obj, "validate_runtime"),
                patch.object(obj, "validate_image"),
                patch.object(obj, "preflight"),
                patch.object(obj, "apply_secret"),
                patch.object(obj, "kubectl") as command,
            ):
                obj.apply()
                payloads = [c.kwargs.get("payload", "") for c in command.call_args_list]
                workloads = next(p for p in payloads if "kind: Deployment" in p)
                self.assertEqual(workloads.count("  replicas: 0"), 9)
                self.assertNotIn("  replicas: 1", workloads)
                self.assertFalse(
                    any(c.args[0] == "rollout" for c in command.call_args_list)
                )


if __name__ == "__main__":
    unittest.main()
