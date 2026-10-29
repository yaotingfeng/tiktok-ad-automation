"""发布门禁测试；Kubernetes/命令运输在边界替身，真实依赖恢复另见 integration。"""

import copy
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy/release"))
from common import CONTEXT, LEGACY_ROLES, NEW_ROLES, PROJECT, ROLES, VPC, Cluster, write
from release import WORKERS, Release, drained, manifest_items, role_environment


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
            "old_sha": "a" * 40,
            "helper": "helper-pod",
            "backup_dir": "/backup/.pending-fixture",
            "database_bytes": 6 * 2**30,
        }

    def approved_sync_values(self):
        policy = {
            "base": {
                "app_max_inflight": 12,
                "endpoint_max_inflight": 3,
                "tenant_max_inflight": 10,
                "advertiser_max_inflight": 4,
                "app_calls_per_window": 10,
                "endpoint_calls_per_window": 3,
                "window_ms": 1000,
                "lease_ms": 960000,
            },
            "endpoints": {
                "ads.get_ads": {"endpoint_max_inflight": 6},
                "reports.material_breakdown": {"endpoint_calls_per_window": 5},
            },
        }
        return {
            "TIKTOK_CALL_POLICIES": json.dumps(policy),
            "TIKTOK_REPORT_DAILY_CALL_BUDGET": "691200",
            "ADS_SYNC_ENABLED": "true",
        }

    def test_sync_runtime_transition_is_scoped_and_preserves_other_policies(self):
        from runtime_migrations import sync_runtime_values

        old = self.approved_sync_values()
        new = sync_runtime_values(old, ["0038_sync_work_indexes"])
        self.assertEqual(old["TIKTOK_REPORT_DAILY_CALL_BUDGET"], "691200")
        self.assertEqual(new["TIKTOK_REPORT_DAILY_CALL_BUDGET"], "0")
        before = json.loads(old["TIKTOK_CALL_POLICIES"])
        after = json.loads(new["TIKTOK_CALL_POLICIES"])
        self.assertEqual(before["base"], after["base"])
        self.assertTrue(before["endpoints"].items() <= after["endpoints"].items())
        self.assertEqual(
            after["endpoints"]["OFFICIAL_API:reports.integrated"],
            {"endpoint_calls_per_window": 10, "endpoint_max_inflight": 6},
        )
        self.assertIs(sync_runtime_values(new, ["0039_sync_service_class"]), new)
        with self.assertRaisesRegex(RuntimeError, "budget differs"):
            sync_runtime_values(
                old | {"TIKTOK_REPORT_DAILY_CALL_BUDGET": "100"},
                ["0038_sync_work_indexes"],
            )
        before["base"]["app_calls_per_window"] = 20
        with self.assertRaisesRegex(RuntimeError, "policy differs"):
            sync_runtime_values(
                old | {"TIKTOK_CALL_POLICIES": json.dumps(before)},
                ["0038_sync_work_indexes"],
            )

    def test_runtime_migration_preserves_archived_source_and_checks_candidate(self):
        import base64

        values = self.approved_sync_values()
        release = self.release
        release.values = values.copy()
        original = "".join(k + "=" + v + "\n" for k, v in values.items())
        write(release.folder / "runtime.env", original.encode())
        write(
            release.folder / "source-fingerprint.json",
            {"migration_heads": ["0038_sync_work_indexes"]},
        )
        olddata = {k: base64.b64encode(v.encode()).decode() for k, v in values.items()}
        write(
            release.folder / "cluster-before.json",
            {
                "secrets": {
                    "runtime": {"data": olddata},
                    "mcp-registration": {"data": {"registration": "fixture"}},
                }
            },
        )
        with self.assertRaisesRegex(RuntimeError, "frozen verified"):
            release.migrate_runtime_values()
        release.state["phase"] = "migration-started"
        release.migrate_runtime_values()
        self.assertEqual((release.folder / "runtime-before.env").read_text(), original)
        self.assertEqual(release.values["TIKTOK_REPORT_DAILY_CALL_BUDGET"], "0")
        candidate = {
            k: base64.b64encode(v.encode()).decode() for k, v in release.values.items()
        }

        def get(kind, name):
            return {
                "data": candidate
                if name.endswith("-runtime")
                else {"registration": "fixture"}
            }

        with patch.object(release.cluster, "get", side_effect=get):
            with self.assertRaisesRegex(RuntimeError, "configuration changed"):
                release.config_unchanged()
            release.state["candidate_applied"] = True
            release.config_unchanged()
            candidate["ADS_SYNC_ENABLED"] = base64.b64encode(b"false").decode()
            with self.assertRaisesRegex(RuntimeError, "configuration changed"):
                release.config_unchanged()

    def test_every_deployment_surface_injects_bounded_worker_pools(self):
        workloads = (ROOT / "deploy/k8s/tk-ada-workloads.yaml").read_text().split("---")
        found = set()
        for block in workloads:
            role = re.search(r"role: (worker-[a-z-]+)", block)
            if role:
                found.add(role.group(1))
                self.assertRegex(block, r'name: DATABASE_POOL_SIZE\n +value: "2"')
                self.assertRegex(block, r'name: DATABASE_MAX_OVERFLOW\n +value: "1"')
        self.assertEqual(found, set(WORKERS))
        for filename in ("compose.yml", "compose.production.yml"):
            content = (ROOT / filename).read_text()
            blocks = re.split(r"(?m)^  (?=worker(?:[-a-z]*):)", content)[1:]
            self.assertTrue(blocks)
            for block in blocks:
                block = re.split(r"(?m)^  [a-z][a-z-]*:", block, maxsplit=1)[0]
                self.assertIn('DATABASE_POOL_SIZE: "2"', block)
                self.assertIn('DATABASE_MAX_OVERFLOW: "1"', block)
        services = list((ROOT / "deploy").glob("staging-*.service"))
        workers = [p for p in services if " worker " in p.read_text()]
        self.assertEqual(len(workers), len(WORKERS))
        for path in workers:
            self.assertIn(
                "ExecStart=/usr/bin/env DATABASE_POOL_SIZE=2 DATABASE_MAX_OVERFLOW=1 ",
                path.read_text(),
            )

    def test_role_database_pool_hash_inputs_and_settings_enforce_all_workers(self):
        shared = {
            "DATABASE_POOL_SIZE": "5",
            "DATABASE_MAX_OVERFLOW": "10",
            "unchanged": "fixture",
        }
        for role in ROLES:
            with self.subTest(role=role):
                environment = role_environment(shared, role, True)
                expected = (
                    {"DATABASE_POOL_SIZE": 2, "DATABASE_MAX_OVERFLOW": 1}
                    if role in WORKERS
                    else {"DATABASE_POOL_SIZE": 5, "DATABASE_MAX_OVERFLOW": 10}
                )
                self.assertEqual({k: int(environment[k]) for k in expected}, expected)
                self.assertEqual(environment["unchanged"], "fixture")
                pod = {"metadata": {"name": role, "labels": {"role": role}}}
                with patch.object(
                    self.release.cluster,
                    "k",
                    return_value=json.dumps(expected).encode(),
                ):
                    self.release.verify_database_pool(pod, environment)
                wrong = dict(expected, DATABASE_POOL_SIZE=99)
                with (
                    patch.object(
                        self.release.cluster,
                        "k",
                        return_value=json.dumps(wrong).encode(),
                    ),
                    self.assertRaisesRegex(RuntimeError, "database pool differs"),
                ):
                    self.release.verify_database_pool(pod, environment)
        self.assertEqual(shared["DATABASE_POOL_SIZE"], "5")
        self.assertEqual(
            role_environment(shared, "worker-ads-reporting", False), shared
        )

    def use_legacy(self):
        self.deployments = {
            k: v
            for k, v in self.deployments.items()
            if k.removeprefix(PROJECT + "-") in LEGACY_ROLES
        }
        self.release.state["deployments"] = copy.deepcopy(self.deployments)

    def test_inventory_accepts_only_complete_old_or_new_contract(self):
        for roles in (
            LEGACY_ROLES,
            ROLES,
            LEGACY_ROLES[:-1],
            (*LEGACY_ROLES, NEW_ROLES[0]),
        ):
            items = [
                dict(
                    copy.deepcopy(self.deployments[PROJECT + "-" + role]),
                    metadata={"name": PROJECT + "-" + role, "labels": {"app": PROJECT}},
                )
                for role in roles
            ]
            with patch.object(
                self.release.cluster, "get", return_value={"items": items}
            ):
                if roles in (LEGACY_ROLES, ROLES):
                    self.assertEqual(
                        len(self.release.cluster.deployments()), len(roles)
                    )
                else:
                    with self.assertRaisesRegex(RuntimeError, "complete legacy"):
                        self.release.cluster.deployments()

    def test_first_upgrade_freezes_only_the_seven_existing_workers(self):
        self.use_legacy()
        pods = [
            {"metadata": {"name": role, "labels": {"role": role}}}
            for role in LEGACY_ROLES
            if role in self.release.original_workers
        ]
        with (
            patch.object(self.release, "assert_lock"),
            patch.object(self.release, "validate_deployments"),
            patch.object(self.release, "helper"),
            patch.object(self.release, "wait_pods"),
            patch.object(self.release, "wait_drained") as drain,
            patch.object(self.release, "probe", return_value={"unacked": 0}),
            patch.object(self.release.cluster, "app_pods", return_value=pods),
            patch.object(self.release.cluster, "scale") as scale,
        ):
            self.release.pause()
        self.assertEqual(len(drain.call_args.args[0]), 7)
        self.assertEqual(
            {call.args[0] for call in scale.call_args_list}, set(self.deployments)
        )
        self.assertEqual(self.release.state["phase"], "frozen")

    def test_first_upgrade_recovery_never_scales_absent_new_workers(self):
        self.use_legacy()
        with (
            patch.object(self.release, "assert_lock"),
            patch.object(
                self.release, "validate_deployments", return_value=self.deployments
            ),
            patch.object(self.release, "config_unchanged"),
            patch.object(self.release, "remove_helper"),
            patch.object(self.release, "accept"),
            patch.object(self.release, "unlock"),
            patch("release.cleanup_completed"),
            patch.object(self.release.cluster, "k"),
            patch.object(self.release.cluster, "scale") as scale,
        ):
            self.release.resume("a" * 40)
        self.assertEqual(
            {call.args[0] for call in scale.call_args_list}, set(self.deployments)
        )

    def test_candidate_missing_new_roles_cannot_resume(self):
        self.use_legacy()
        current = copy.deepcopy(self.deployments)
        for obj in current.values():
            obj["spec"]["template"]["spec"]["containers"][0]["image"] = VPC + "b" * 40
        with (
            patch.object(self.release, "assert_lock"),
            patch.object(self.release, "validate_deployments", return_value=current),
            patch.object(self.release.cluster, "scale") as scale,
            self.assertRaisesRegex(RuntimeError, "role set"),
        ):
            self.release.resume("b" * 40)
        scale.assert_not_called()

    def test_added_roles_block_old_template_recovery(self):
        current = copy.deepcopy(self.deployments)
        self.use_legacy()
        with (
            patch.object(self.release.cluster, "deployments", return_value=current),
            self.assertRaisesRegex(RuntimeError, "role set"),
        ):
            self.release.validate_deployments(original=True)

    def test_broker_migration_failure_blocks_automatic_old_recovery(self):
        for phase in ("broker-migration-started", "broker-migrated"):
            self.release.state["phase"] = phase
            with (
                patch.object(self.release, "load"),
                self.assertRaisesRegex(RuntimeError, "forbidden"),
            ):
                self.release.recover()

    def test_broker_migration_requires_verified_candidate_and_sets_boundary_first(self):
        from common import digest

        write(self.release.folder / "manifest.json", {"fixture": True})
        proof = {
            "postgres_matches": True,
            "redis": {},
            "manifest_sha256": digest(self.release.folder / "manifest.json"),
            "candidate_sha": "b" * 40,
        }
        write(self.release.folder / "restore-verified.json", proof)
        self.release.state.update(phase="backup-verified", candidate_sha="b" * 40)

        def migrate(*args):
            self.assertEqual(self.release.state["phase"], "broker-migration-started")
            return {"moved": 1750, "verified": True}

        with patch.object(self.release, "probe", side_effect=migrate):
            self.release.migrate_targeted_queue()
        self.assertEqual(self.release.state["phase"], "broker-migrated")
        self.assertEqual(
            json.loads((self.release.folder / "broker-migration.json").read_text()),
            {"moved": 1750, "verified": True},
        )
        self.release.state["phase"] = "backup-verified"
        proof["candidate_sha"] = "c" * 40
        write(self.release.folder / "restore-verified.json", proof)
        with (
            patch.object(self.release, "probe") as probe,
            self.assertRaisesRegex(RuntimeError, "Verified"),
        ):
            self.release.migrate_targeted_queue()
        probe.assert_not_called()

    def test_new_roles_require_frozen_published_verified_backup(self):
        from common import digest

        spec = importlib.util.spec_from_file_location(
            "role_operations", ROOT / "deploy/k8s/operations.py"
        )
        operations = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(operations)
        self.use_legacy()
        for value in self.deployments.values():
            value["spec"]["replicas"] = 0
        write(self.release.folder / "manifest.json", {"fixture": True})
        checksum = digest(self.release.folder / "manifest.json")
        state = dict(
            self.release.state, phase="migration-started", candidate_sha="b" * 40
        )
        proof = {
            "manifest_sha256": checksum,
            "candidate_sha": "b" * 40,
            "postgres_matches": True,
            "redis": {},
        }
        write(self.release.folder / "state.json", state)
        write(self.release.folder / "restore-verified.json", proof)
        write(
            self.release.folder / "cluster-backup.json", {"manifest_sha256": checksum}
        )
        with patch.dict(
            os.environ,
            INGRESS_HOST="tkada.shortclipflix.com",
            RELEASE_HOLD_WORKLOADS="1",
            RELEASE_STATE_DIR=str(self.release.folder),
        ):
            obj = operations.Deployment()
            obj.release = "b" * 40
            obj.existing_deployments = self.deployments
            with patch.object(
                obj,
                "get",
                return_value={
                    "metadata": {"uid": "lock"},
                    "data": {"token": "fixture"},
                },
            ):
                obj.guard_role_upgrade()
                with (
                    patch.dict(os.environ, RELEASE_HOLD_WORKLOADS="0"),
                    self.assertRaisesRegex(ValueError, "frozen"),
                ):
                    obj.guard_role_upgrade()
                self.deployments[PROJECT + "-api"]["spec"]["replicas"] = 1
                with self.assertRaisesRegex(ValueError, "remain frozen"):
                    obj.guard_role_upgrade()
                self.deployments[PROJECT + "-api"]["spec"]["replicas"] = 0
                proof["manifest_sha256"] = "bad"
                write(self.release.folder / "restore-verified.json", proof)
                with self.assertRaisesRegex(ValueError, "published candidate backup"):
                    obj.guard_role_upgrade()

    def test_kubectl_manifest_stream_and_list_are_equivalent(self):
        items = [
            {"kind": "ConfigMap", "data": {"flag": "false"}},
            {"kind": "Deployment"},
        ]
        stream = "\n".join(json.dumps(item, indent=2) for item in items).encode()
        self.assertEqual(manifest_items(stream), items)
        self.assertEqual(
            manifest_items(json.dumps({"kind": "List", "items": items}).encode()), items
        )

    def test_prepare_never_pulls_images_on_the_release_host(self):
        # 恢复验证已移到集群内；Jenkins 节点不再下载数 GB 的应用镜像。
        with (
            patch("release.run", return_value=b"") as command,
            patch.object(
                self.release, "image_digest", side_effect=RuntimeError("stop")
            ),
            self.assertRaisesRegex(RuntimeError, "stop"),
        ):
            self.release.prepare_tools("b" * 40)
        self.assertFalse(
            any(c.args[0][:2] == ["docker", "pull"] for c in command.call_args_list)
        )

    def test_image_digest_uses_registry_index_reported_by_ack(self):
        index = "sha256:" + "1" * 64
        child = "sha256:" + "2" * 64
        descriptor = {
            "digest": index,
            "manifests": [
                {"digest": child, "platform": {"architecture": "amd64", "os": "linux"}}
            ],
        }
        with patch("release.run", return_value=json.dumps(descriptor).encode()):
            self.assertEqual(self.release.image_digest("a" * 40), index)

    def helper_spec(self, candidate):
        self.release.state["candidate_sha"] = candidate
        self.release.state["deployments"][PROJECT + "-beat"]["spec"]["template"][
            "spec"
        ] = {
            "containers": [{"name": "beat", "image": VPC + "a" * 40, "args": ["x"]}],
            "volumes": [{"name": "beat-data"}],
        }
        created = {}

        def create(obj):
            created.update(obj)
            return {"metadata": {"uid": "helper"}}

        with (
            patch.object(self.release.cluster, "create", side_effect=create),
            patch.object(self.release.cluster, "k"),
            patch("release.check_space", return_value={}) as space,
        ):
            self.release.helper()
        space.assert_called_once()
        return created["spec"]

    def test_helper_mounts_backup_disk_and_sized_verify_disk(self):
        spec = self.helper_spec("b" * 40)
        containers = {c["name"]: c for c in spec["containers"]}
        self.assertEqual(
            set(containers),
            {"app", "postgres", "verify-pg", "verify-redis", "candidate"},
        )
        self.assertEqual(containers["candidate"]["image"], VPC + "b" * 40)
        volumes = {v["name"]: v for v in spec["volumes"]}
        self.assertEqual(
            volumes["backup"]["persistentVolumeClaim"]["claimName"],
            PROJECT + "-backup",
        )
        # 6 GiB 数据库按 2.5 倍再加 5 GiB：20 GiB 下限之上。
        self.assertEqual(
            volumes["verify"]["ephemeral"]["volumeClaimTemplate"]["spec"]["resources"][
                "requests"
            ]["storage"],
            "20Gi",
        )
        self.assertIn("beat-data", volumes)
        self.assertIn("listen_addresses=127.0.0.1", containers["verify-pg"]["args"])
        self.assertIn("127.0.0.1", containers["verify-redis"]["args"])
        mounts = {m["name"]: m for m in containers["verify-pg"]["volumeMounts"]}
        self.assertTrue(mounts["backup"]["readOnly"])
        self.assertEqual(self.release.state["phase"], "helper-ready")

    def test_same_version_backup_has_no_candidate_container(self):
        self.release.state["database_bytes"] = 40 * 2**30
        spec = self.helper_spec(None)
        self.assertNotIn("candidate", {c["name"] for c in spec["containers"]})
        verify = next(v for v in spec["volumes"] if v["name"] == "verify")
        self.assertEqual(
            verify["ephemeral"]["volumeClaimTemplate"]["spec"]["resources"]["requests"][
                "storage"
            ],
            "105Gi",
        )

    def test_backup_disk_created_only_when_missing(self):
        claim = {
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": PROJECT + "-backup"},
        }
        for existing, applies in (([], True), ([claim], False)):
            write(
                self.release.folder / "cluster-before.json",
                {"persistentvolumeclaims": {"items": existing}},
            )
            with patch.object(self.release.cluster, "k") as command:
                self.release.ensure_backup_disk([{"kind": "ConfigMap"}, claim])
            self.assertEqual(command.called, applies)

    def test_insufficient_backup_space_stops_before_freeze(self):
        from backup import check_space

        def pod(*_args, **_kwargs):
            return b"%d\n%d\n" % (3 * 2**30, 2 * 2**30)

        with self.assertRaisesRegex(RuntimeError, "expand"):
            check_space(pod, 6 * 2**30)
        self.assertEqual(
            check_space(lambda *_a, **_k: b"%d\n0\n" % (3 * 2**30), 6 * 2**30)[
                "required"
            ],
            2 * 2**30,
        )
        # 生产回归：上一份 18.9 GiB、50Gi 盘剩 31 GiB 时原规则要求 56.7 GiB。
        # 盘上的 latest 已不在空闲里，只需新的一份加一份余量。
        last = 18898 * 2**20
        self.assertEqual(
            check_space(lambda *_a, **_k: b"%d\n%d\n" % (81 * 2**30, last), 56 * 2**30)[
                "required"
            ],
            2 * last,
        )

    def test_resume_starts_beat_after_all_consumers_ready(self):
        calls = []
        current = copy.deepcopy(self.deployments)
        for item in current.values():
            item["spec"]["template"]["spec"]["containers"][0]["image"] = VPC + "b" * 40
        with (
            patch.object(self.release, "assert_lock"),
            patch.object(self.release, "validate_deployments", return_value=current),
            patch.object(self.release, "config_unchanged"),
            patch.object(self.release, "remove_helper"),
            patch.object(self.release, "accept"),
            patch.object(self.release, "unlock"),
            patch("release.cleanup_completed"),
            patch.object(
                self.release.cluster,
                "scale",
                side_effect=lambda name, _n: calls.append(("scale", name)),
            ),
            patch.object(
                self.release.cluster,
                "k",
                side_effect=lambda *a, **_k: calls.append(("wait", a[1])),
            ),
        ):
            self.release.resume("b" * 40)
        beat = calls.index(("scale", PROJECT + "-beat"))
        self.assertEqual([c for c in calls[:beat] if c[0] == "scale"].__len__(), 10)
        self.assertEqual([c for c in calls[:beat] if c[0] == "wait"].__len__(), 10)
        # 前十个先全部扩容，再统一等待就绪。
        self.assertTrue(all(c[0] == "scale" for c in calls[:10]))

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

    def test_kubectl_retries_only_safe_connection_failures(self):
        from common import kubectl_retryable

        unsent = 'error: error sending request: Post "https://x/exec": EOF'
        self.assertTrue(kubectl_retryable(("create", "-f", "-"), unsent))
        self.assertTrue(
            kubectl_retryable(("exec", "p"), "net/http: TLS handshake timeout")
        )
        # 送达与否不明确：只读命令或声明可重放的 exec 才重试。
        self.assertTrue(kubectl_retryable(("get", "pods"), "error: EOF"))
        self.assertTrue(
            kubectl_retryable(("create", "--dry-run=client", "-f", "x"), "error: EOF")
        )
        self.assertFalse(kubectl_retryable(("exec", "p", "--", "mv"), "error: EOF"))
        self.assertTrue(kubectl_retryable(("exec", "p"), "error: EOF", idempotent=True))
        # 容器内命令自身失败，即使输出含连接字样也不重试。
        self.assertFalse(
            kubectl_retryable(
                ("exec", "p"),
                "dial tcp: refused\ncommand terminated with exit code 2",
                idempotent=True,
            )
        )

    def test_kubectl_retry_is_bounded_and_returns_success(self):
        import common

        flaky = [
            subprocess.CompletedProcess([], 1, b"", b"error: EOF"),
            subprocess.CompletedProcess([], 0, b"ok", b""),
        ]
        with (
            patch("common.subprocess.run", side_effect=flaky) as command,
            patch("common.time.sleep"),
        ):
            self.assertEqual(Cluster().k("get", "pods"), b"ok")
            self.assertEqual(command.call_count, 2)
        failing = subprocess.CompletedProcess([], 1, b"", b"error: EOF")
        with (
            patch("common.subprocess.run", return_value=failing) as command,
            patch("common.time.sleep"),
        ):
            with self.assertRaises(RuntimeError):
                Cluster().k("get", "pods")
            self.assertEqual(command.call_count, common.KUBECTL_ATTEMPTS)
        with (
            patch("common.subprocess.run", return_value=failing) as command,
            patch("common.time.sleep"),
        ):
            with self.assertRaises(RuntimeError):
                Cluster().k("exec", "p", "--", "mv", "a", "b")
            self.assertEqual(command.call_count, 1)

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

    def test_command_failure_keeps_diagnostics_private(self):
        import subprocess

        from common import run

        log = self.release.folder / "command-error.json"
        with (
            patch.dict(os.environ, RELEASE_DIAGNOSTIC_LOG=str(log)),
            patch(
                "common.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    ["fixture"], 1, b"", b"private-fixture"
                ),
            ),
            self.assertRaisesRegex(RuntimeError, "fixture") as caught,
        ):
            run(["fixture"])
        self.assertNotIn("private-fixture", str(caught.exception))
        self.assertEqual(json.loads(log.read_text())["stderr"], "private-fixture")
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)

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

    def test_routine_preserves_unacked_after_drain_but_migration_still_blocks(self):
        pods = [
            {"metadata": {"name": role, "labels": {"role": role}}} for role in WORKERS
        ]
        for mode in ("routine", "full"):
            self.release.state["mode"] = mode
            with (
                patch.object(self.release, "assert_lock"),
                patch.object(self.release, "validate_deployments"),
                patch.object(self.release, "wait_pods"),
                patch.object(self.release, "wait_drained") as drained,
                patch.object(
                    self.release, "probe", return_value={"unacked": 24}
                ) as probe,
                patch.object(self.release.cluster, "app_pods", return_value=pods),
                patch.object(self.release.cluster, "scale"),
            ):
                if mode == "routine":
                    self.release.pause()
                    self.assertEqual(self.release.state["phase"], "frozen")
                else:
                    with self.assertRaisesRegex(RuntimeError, "Unacknowledged"):
                        self.release.pause()
                drained.assert_called_once()
                self.assertEqual(
                    [c.args[0] for c in probe.call_args_list], ["cancel", "broker"]
                )
                self.assertEqual(
                    json.loads(
                        (self.release.folder / "broker-frozen.json").read_text()
                    ),
                    {"unacked": 24},
                )

    def test_failed_backup_does_not_migrate_or_resume(self):
        with (
            patch.object(self.release, "begin"),
            patch.object(self.release, "prepare_tools"),
            patch.object(self.release, "check_remote"),
            patch.object(self.release, "pause"),
            patch("release.snapshot"),
            patch("release.restore_verify", side_effect=RuntimeError("bad backup")),
            patch("release.publish") as upload,
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

    def test_creative_contract_guard_blocks_before_snapshot_and_allows_recheck(self):
        marker = b"backend/app/alembic/versions/0060_frozen_creative_units.py\n"
        with (
            patch("release.run", return_value=marker),
            patch.object(
                self.release,
                "probe",
                side_effect=[
                    {
                        "drained": False,
                        "blockers": {"step_runnable_or_unknown": "fixture"},
                    },
                    {"drained": True, "blockers": {}},
                ],
            ) as probe,
        ):
            with self.assertRaisesRegex(
                RuntimeError, "Old creative-contract work remains"
            ):
                self.release.require_creative_unit_drain("b" * 40)
            self.release.require_creative_unit_drain("b" * 40)
            self.assertEqual(
                [c.args for c in probe.call_args_list], [("creative-unit-drain",)] * 2
            )
        self.assertTrue(
            json.loads((self.release.folder / "creative-unit-drain.json").read_text())[
                "drained"
            ]
        )

    def test_contract_cutover_failure_precedes_backup_and_migration(self):
        with (
            patch.object(self.release, "begin"),
            patch.object(self.release, "prepare_tools"),
            patch.object(self.release, "check_remote"),
            patch.object(self.release, "pause") as pause,
            patch.object(
                self.release,
                "require_creative_unit_drain",
                side_effect=RuntimeError("old UNKNOWN"),
            ),
            patch("release.snapshot") as snapshot,
            patch.object(self.release, "resume") as resume,
        ):
            with self.assertRaisesRegex(RuntimeError, "old UNKNOWN"):
                self.release.execute("deploy", "b" * 40, full_backup=True)
            pause.assert_called_once()
            snapshot.assert_not_called()
            resume.assert_not_called()

    def test_contract_guard_abort_recovers_old_image_without_task_mutation(self):
        # guard 停在迁移前 frozen：recover 只正常恢复原程序，持久任务留给原消费者。
        self.release.state["phase"] = "frozen"
        self.release.state["image_digests"] = {"a" * 40: "sha256:old"}
        with (
            patch.object(self.release, "load"),
            patch.object(self.release, "validate_deployments"),
            patch.object(self.release, "config_unchanged"),
            patch.object(self.release, "resume") as resume,
            patch.object(self.release, "probe") as probe,
            patch("release.run") as command,
        ):
            self.release.recover()
            resume.assert_called_once_with("a" * 40)
            probe.assert_not_called()
            command.assert_not_called()

    def test_failed_migration_keeps_zero_replicas_for_inspection(self):
        with (
            patch("release.needs_full_backup", return_value=True),
            patch.object(self.release, "begin"),
            patch.object(self.release, "prepare_tools"),
            patch.object(self.release, "check_remote"),
            patch.object(self.release, "pause"),
            patch.object(self.release, "require_creative_unit_drain"),
            patch("release.snapshot"),
            patch("release.restore_verify"),
            patch("release.publish"),
            patch.object(self.release, "assert_lock"),
            patch.object(self.release, "config_unchanged"),
            patch.object(self.release.cluster, "app_pods", return_value=[]),
            patch.object(self.release, "migrate_targeted_queue"),
            patch.object(self.release, "migrate_runtime_values"),
            patch("release.run", side_effect=RuntimeError("migration failed")),
            patch.object(self.release, "resume") as resume,
        ):
            self.release.state["old_sha"] = "a" * 40
            with self.assertRaises(RuntimeError):
                self.release.execute("deploy", "b" * 40, full_backup=True)
            self.assertEqual(self.release.state["phase"], "migration-started")
            resume.assert_not_called()

    def test_default_deploy_skips_all_backup_steps(self):
        events = []
        with (
            patch.object(self.release, "begin"),
            patch.object(self.release, "prepare_tools"),
            patch.object(self.release, "check_remote"),
            patch("release.needs_full_backup", return_value=False),
            patch.object(self.release, "helper"),
            patch.object(
                self.release, "probe", return_value={"migration_heads": ["v1"]}
            ),
            patch("release.snapshot", side_effect=AssertionError("backup called")),
            patch(
                "release.restore_verify", side_effect=AssertionError("restore called")
            ),
            patch.object(
                self.release, "pause", side_effect=lambda: events.append("freeze")
            ),
            patch.object(
                self.release,
                "apply_images",
                side_effect=lambda: events.append("images"),
            ),
            patch.object(
                self.release, "resume", side_effect=lambda *a: events.append("resume")
            ),
        ):
            self.release.execute("deploy", "b" * 40)
        self.assertEqual(events, ["freeze", "images", "resume"])
        proof = json.loads((self.release.folder / "release-policy.json").read_text())
        self.assertEqual(proof["candidate_sha"], "b" * 40)
        self.assertEqual(proof["backup"], "not-created")
        self.assertEqual(proof["restore_verification"], "not-performed")
        self.assertEqual(proof["migration_heads"], ["v1"])

    def test_no_backup_rejects_data_contract_changes_before_preparing_tools(self):
        with (
            patch.object(self.release, "begin"),
            patch.object(
                self.release,
                "prepare_tools",
                side_effect=AssertionError("prepared unsafe release"),
            ),
            patch("release.needs_full_backup", return_value=True),
            patch.object(self.release, "unlock"),
            self.assertRaisesRegex(RuntimeError, "explicit release plan"),
        ):
            self.release.execute("deploy", "b" * 40)

    def test_no_backup_image_switch_requires_matching_release_waiver(self):
        release = self.release
        release.state.update(
            mode="routine", candidate_sha="b" * 40, backup_policy="none"
        )
        for name, deployment in self.deployments.items():
            deployment["spec"]["replicas"] = 0
            deployment["spec"]["template"]["spec"]["containers"][0]["name"] = name
        waiver = {
            "token": "fixture",
            "old_sha": "a" * 40,
            "candidate_sha": "c" * 40,
            "backup": "not-created",
            "restore_verification": "not-performed",
        }
        write(release.folder / "release-policy.json", waiver)

        def switch(*args):
            self.assertEqual(args[:2], ("set", "image"))
            name = args[2].removeprefix("deployment/")
            self.deployments[name]["spec"]["template"]["spec"]["containers"][0][
                "image"
            ] = args[3].split("=", 1)[1]

        with (
            patch.object(release, "assert_lock"),
            patch.object(release, "config_unchanged"),
            patch.object(
                release, "validate_deployments", return_value=self.deployments
            ),
            patch.object(release.cluster, "app_pods", return_value=[]),
            patch("release.needs_full_backup", return_value=False),
            patch.object(release.cluster, "k", side_effect=switch),
        ):
            with self.assertRaisesRegex(RuntimeError, "policy does not match"):
                release.apply_images()
            self.assertEqual(
                {
                    d["spec"]["template"]["spec"]["containers"][0]["image"]
                    for d in self.deployments.values()
                },
                {VPC + "a" * 40},
            )
            waiver["candidate_sha"] = "b" * 40
            write(release.folder / "release-policy.json", waiver)
            release.apply_images()
        self.assertEqual(
            {
                d["spec"]["template"]["spec"]["containers"][0]["image"]
                for d in self.deployments.values()
            },
            {VPC + "b" * 40},
        )
        self.assertTrue(release.state["candidate_applied"])
        self.assertEqual(release.state["phase"], "candidate-held")

    def test_no_backup_helper_needs_no_backup_disk_or_restore_services(self):
        self.release.state.update(mode="routine", backup_policy="none")
        created = {}

        def create(obj):
            created.update(obj)
            return {"metadata": {"uid": "helper"}}

        with (
            patch.object(self.release.cluster, "create", side_effect=create),
            patch.object(self.release.cluster, "k"),
            patch(
                "release.check_space",
                side_effect=AssertionError("backup disk accessed"),
            ),
        ):
            self.release.helper()
        spec = created["spec"]
        self.assertEqual([c["name"] for c in spec["containers"]], ["app"])
        self.assertFalse(
            any(
                "persistentVolumeClaim" in v or "ephemeral" in v
                for v in spec["volumes"]
            )
        )
        self.assertNotIn(
            "backup", {v["name"] for v in spec["containers"][0]["volumeMounts"]}
        )

    def test_online_backup_never_pauses_or_restarts_application(self):
        with (
            patch.object(self.release, "begin"),
            patch.object(self.release, "prepare_tools"),
            patch.object(self.release, "helper"),
            patch("release.snapshot") as snapshot,
            patch("release.restore_verify") as restore,
            patch("release.publish"),
            patch.object(self.release, "remove_helper"),
            patch.object(self.release, "unlock"),
            patch.object(self.release, "pause") as pause,
            patch.object(self.release, "resume") as resume,
        ):
            self.release.execute("backup-online", None)
            snapshot.assert_called_once_with(self.release, online=True)
            self.assertTrue(restore.call_args.kwargs["online"])
            pause.assert_not_called()
            resume.assert_not_called()

    def test_failed_online_backup_releases_lock_before_publish(self):
        # 生产回归：每日在线备份失败留下集群锁，后续发布全部被阻断。
        for failing in ("helper", "snapshot", "restore"):
            with (
                patch.object(self.release, "begin"),
                patch.object(self.release, "prepare_tools"),
                patch.object(self.release, "save"),
                patch.object(
                    self.release,
                    "helper",
                    side_effect=RuntimeError("x") if failing == "helper" else None,
                ),
                patch(
                    "release.snapshot",
                    side_effect=RuntimeError("x") if failing == "snapshot" else None,
                ),
                patch(
                    "release.restore_verify",
                    side_effect=RuntimeError("x") if failing == "restore" else None,
                ),
                patch("release.publish") as publish,
                patch.object(self.release, "remove_helper") as remove,
                patch.object(self.release, "unlock") as unlock,
            ):
                self.release.state = {"backup_dir": "/backup/.pending-x", "helper": "h"}
                with self.assertRaises(RuntimeError):
                    self.release.execute("backup-online", None)
                remove.assert_called_once()
                unlock.assert_called_once()
                publish.assert_not_called()

    def test_online_backup_publish_failure_keeps_lock_for_inspection(self):
        with (
            patch.object(self.release, "begin"),
            patch.object(self.release, "prepare_tools"),
            patch.object(self.release, "save"),
            patch.object(self.release, "helper"),
            patch("release.snapshot"),
            patch("release.restore_verify"),
            patch("release.publish", side_effect=RuntimeError("rotate")),
            patch.object(self.release, "remove_helper") as remove,
            patch.object(self.release, "unlock") as unlock,
        ):
            self.release.state = {"backup_dir": "/backup/.pending-x", "helper": "h"}
            with self.assertRaises(RuntimeError):
                self.release.execute("backup-online", None)
            remove.assert_not_called()
            unlock.assert_not_called()

    def test_lightweight_and_online_helpers_do_not_mount_live_beat_disk(self):
        for mode, action in [("routine", "deploy"), ("full", "backup-online")]:
            self.release.state.update(mode=mode, action=action)
            spec = self.helper_spec(None)
            self.assertNotIn("beat-data", {v["name"] for v in spec["volumes"]})
            if mode == "routine":
                self.assertEqual([c["name"] for c in spec["containers"]], ["app"])
                self.assertNotIn("verify", {v["name"] for v in spec["volumes"]})

    def test_routine_partial_update_stays_frozen_and_can_finish(self):
        for name, d in self.deployments.items():
            d["spec"]["replicas"] = 0
            d["spec"]["template"]["spec"]["containers"][0]["name"] = name
        self.release.state.update(
            mode="routine", candidate_sha="b" * 40, backup_policy="none"
        )
        write(
            self.release.folder / "release-policy.json",
            {
                "token": "fixture",
                "old_sha": "a" * 40,
                "candidate_sha": "b" * 40,
                "backup": "not-created",
                "restore_verification": "not-performed",
            },
        )
        with (
            patch.object(self.release, "assert_lock"),
            patch.object(self.release, "config_unchanged"),
            patch.object(
                self.release, "validate_deployments", return_value=self.deployments
            ),
            patch.object(self.release.cluster, "app_pods", return_value=[]),
            patch("release.needs_full_backup", return_value=False),
            patch.object(
                self.release.cluster, "k", side_effect=RuntimeError("transport")
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "transport"):
                self.release.apply_images()
            self.assertEqual(self.release.state["phase"], "image-update-started")
        self.assertFalse(self.release.state.get("candidate_applied"))

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
                self.assertEqual(workloads.count("  replicas: 0"), 11)
                self.assertNotIn("  replicas: 1", workloads)
                self.assertFalse(
                    any(c.args[0] == "rollout" for c in command.call_args_list)
                )


class SupersededTests(unittest.TestCase):
    """Jenkins 排队期间 main 前进：本批在检查前退出，本机推送前的落后远端不受影响。"""

    def test_only_an_ancestor_of_company_main_is_superseded(self):
        import subprocess

        spec = importlib.util.spec_from_file_location("local", ROOT / "deploy/local.py")
        local = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(local)
        with tempfile.TemporaryDirectory() as tmp:
            company, work = Path(tmp) / "company.git", Path(tmp) / "work"

            def git(*args, cwd=work):
                return subprocess.run(
                    ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
                ).stdout.strip()

            subprocess.run(["git", "init", "-q", "--bare", str(company)], check=True)
            subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
            git("remote", "add", "company", str(company))
            shas = []
            for n in range(2):
                git(
                    "-c",
                    "user.name=t",
                    "-c",
                    "user.email=t@t",
                    "commit",
                    "-q",
                    "--allow-empty",
                    "-m",
                    str(n),
                )
                shas.append(git("rev-parse", "HEAD"))
            git("push", "-q", "company", "main")
            older, newer = shas
            self.assertTrue(local.superseded(older, newer, work))
            self.assertFalse(local.superseded(newer, newer, work))
            self.assertFalse(local.superseded(newer, older, work))


if __name__ == "__main__":
    unittest.main()
