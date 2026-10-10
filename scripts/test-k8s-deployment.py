"""Deployment contracts; fake only the external kubectl transport, never a cluster."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SHA = "0123456789abcdef0123456789abcdef01234567"
IMAGE = "junbo-image.tencentcloudcr.com/prod/tiktok-ad-automation:" + SHA
PROJECT = "tiktok-ad-automation"


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.envfile = self.root / "runtime.env"
        self.values = {
            "SECRET_KEY": "fixture-signing-key",
            "FIRST_SUPERUSER": "admin",
            "FIRST_SUPERUSER_PASSWORD": "fixture-password",
            "DATABASE_URL": "postgresql://user:password@db.internal:5432/test",
            "REDIS_URL": "redis://:password@queue.internal:6379/1",
            "CONNECTION_ENCRYPTION_KEY": "fixture-encryption-key",
            "S3_ENDPOINT_URL": "https://fixture.r2.cloudflarestorage.com",
            "S3_BUCKET": "fixture",
            "S3_ACCESS_KEY_ID": "fixture-access",
            "S3_SECRET_ACCESS_KEY": "fixture-secret",
            "TIKTOK_CALL_POLICIES": '{"base":{"window_seconds":1}}',
            "FRONTEND_HOST": "https://tiktok-ad-automation-test.jetmobo.com",
            "MCP_REDIRECT_URI": "https://tiktok-ad-automation-test.jetmobo.com/api/integrations/tiktok/mcp/callback",
            "MATERIAL_INGEST_ENABLED": "false",
            "MATERIAL_CLEANUP_ENABLED": "false",
            "ADS_SYNC_ENABLED": "false",
        }
        registration = self.root / "mcp.json"
        registration.write_text('{"client_id":"fixture"}')
        self.log = self.root / "calls.jsonl"
        self.state = self.root / "state.json"
        self.state.write_text(json.dumps({"items": []}))
        binary = self.root / "kubectl"
        binary.write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
state = json.loads(pathlib.Path(os.environ['FAKE_STATE']).read_text())
payload = sys.stdin.read() if '-f' in args and args[args.index('-f') + 1] == '-' else ''
with open(os.environ['FAKE_LOG'], 'a') as f:
    f.write(json.dumps({'args': args, 'payload': payload}) + '\\n')
if 'get' in args:
    kind = args[args.index('get') + 1]
    if kind == 'ingress' and 'prod-https' in args:
        print(json.dumps(state.get('ingress', {'metadata': {'resourceVersion': '1'}, 'spec': {'tls': [{'hosts': ['*.jetmobo.com']}], 'rules': [{'host': 'other.jetmobo.com'}]}})))
    elif kind == 'namespace':
        print('{}')
    elif kind == 'secret':
        if 'secret' in state:
            print(json.dumps(state['secret']))
        elif 'tiktok-ad-automation-registry' in args and not state.get('missing_registry'):
            print(json.dumps({'metadata': {'name': 'tiktok-ad-automation-registry', 'labels': {'app': 'tiktok-ad-automation'}}, 'type': 'kubernetes.io/dockerconfigjson', 'data': {'.dockerconfigjson': 'e30='}}))
        else:
            print('')
    elif kind == 'service':
        print(json.dumps({'metadata': {'labels': {'app': 'tiktok-ad-automation'}}, 'spec': {'selector': {'app': 'tiktok-ad-automation', 'role': 'api'}, 'ports': [{'port': 80, 'targetPort': 80}]}}))
    elif kind == 'job':
        print('')
    else:
        print(json.dumps({'items': state['items']}))
elif 'patch' in args:
    patch = json.loads(args[args.index('--patch') + 1])
    ingress = state.get('ingress', {'metadata': {'resourceVersion': '1'}, 'spec': {'tls': [{'hosts': ['*.jetmobo.com']}], 'rules': [{'host': 'other.jetmobo.com'}]}})
    ingress['spec']['rules'].append(patch[-1]['value'])
    state['ingress'] = ingress
    pathlib.Path(os.environ['FAKE_STATE']).write_text(json.dumps(state))
    print('{}')
else:
    print('{}')
""")
        binary.chmod(0o755)
        docker = self.root / "docker"
        docker.write_text(
            "#!/usr/bin/env python3\nimport os, sys\nif sys.argv[1] == 'image': print(os.environ.get('IMAGE_REVISION', os.environ['RELEASE_ID']))\nelif sys.argv[1] == 'run': sys.exit(int(os.environ.get('RUNTIME_CHECK_EXIT', '0')))\n"
        )
        docker.chmod(0o755)
        self.env = dict(
            os.environ,
            PATH=str(self.root) + os.pathsep + os.environ["PATH"],
            FAKE_STATE=str(self.state),
            FAKE_LOG=str(self.log),
            K8S_NAMESPACE="prod",
            K8S_CONTEXT="fixture-context",
            INGRESS_HOST="tiktok-ad-automation-test.jetmobo.com",
            RELEASE_ID=SHA,
            APP_IMAGE=IMAGE,
            RUNTIME_ENV_FILE=str(self.envfile),
            MCP_CLIENT_REGISTRATION_FILE=str(registration),
        )

    def run_script(self, script="apply.sh", dry=True, **extra):
        self.envfile.write_text("\n".join(f"{k}={v}" for k, v in self.values.items()))
        return subprocess.run(
            ["bash", str(REPO / "deploy/k8s" / script)],
            env=dict(self.env, DRY_RUN="1" if dry else "0", **extra),
            text=True,
            capture_output=True,
            check=False,
        )

    def calls(self):
        return (
            [json.loads(line) for line in self.log.read_text().splitlines()]
            if self.log.exists()
            else []
        )

    def assert_no_writes(self):
        self.assertFalse(
            [
                c
                for c in self.calls()
                if any(x in c["args"] for x in ("apply", "patch", "create", "delete"))
            ]
        )

    def test_reject_mutable_or_foreign_or_mismatched_image(self):
        for image in (
            "unrelated:latest",
            IMAGE.rsplit(":", 1)[0] + ":latest",
            IMAGE[:-1] + "8",
        ):
            with self.subTest(image=image):
                result = self.run_script(APP_IMAGE=image)
                self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assert_no_writes()

    def test_require_explicit_context_and_host(self):
        for extra in ({"K8S_CONTEXT": ""}, {"INGRESS_HOST": ""}):
            self.assertNotEqual(self.run_script(**extra).returncode, 0)
        self.assert_no_writes()

    def custom_domain(self):
        self.env["INGRESS_HOST"] = "tkada.galaxyshortdrama.com"
        self.values["FRONTEND_HOST"] = "https://" + self.env["INGRESS_HOST"]
        self.values["MCP_REDIRECT_URI"] = (
            self.values["FRONTEND_HOST"] + "/api/integrations/tiktok/mcp/callback"
        )

    def test_custom_domain_requires_matching_tls_before_any_writes(self):
        self.custom_domain()
        for script in ("apply.sh", "patch-public-ingress.sh"):
            result = self.run_script(script, dry=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("TLS hosts do not cover", result.stderr)
            self.assert_no_writes()

    def test_custom_domain_accepts_exact_or_single_level_wildcard_tls(self):
        self.custom_domain()
        for host in ("tkada.galaxyshortdrama.com", "*.galaxyshortdrama.com"):
            self.state.write_text(
                json.dumps(
                    {
                        "items": [],
                        "ingress": {
                            "metadata": {"resourceVersion": "1"},
                            "spec": {
                                "tls": [{"hosts": [host]}],
                                "rules": [{"host": "other.jetmobo.com"}],
                            },
                        },
                    }
                )
            )
            result = self.run_script(dry=False, PREFLIGHT_ONLY="1")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assert_no_writes()

    def test_parent_wildcard_cannot_cover_multiple_domain_levels(self):
        self.custom_domain()
        self.state.write_text(
            json.dumps(
                {
                    "items": [],
                    "ingress": {
                        "spec": {
                            "tls": [{"hosts": ["*.com"]}],
                            "rules": [{"host": "other.jetmobo.com"}],
                        },
                    },
                }
            )
        )
        self.assertNotEqual(self.run_script(dry=False).returncode, 0)
        self.assert_no_writes()

    def test_reject_callback_from_another_environment(self):
        self.values["MCP_REDIRECT_URI"] = (
            "https://old.example/api/integrations/tiktok/mcp/callback"
        )
        self.assertNotEqual(self.run_script().returncode, 0)
        self.assert_no_writes()

    def test_reject_implicit_flags_and_empty_policy(self):
        del self.values["ADS_SYNC_ENABLED"]
        self.assertNotEqual(self.run_script().returncode, 0)
        self.values["ADS_SYNC_ENABLED"] = "false"
        self.values["TIKTOK_CALL_POLICIES"] = "{}"
        self.assertNotEqual(self.run_script().returncode, 0)

    def test_reject_loopback_without_password(self):
        for url in ("redis://localhost:6379/0", "redis://[::1]:6379/0"):
            self.values["REDIS_URL"] = url
            self.assertNotEqual(self.run_script().returncode, 0)

    def test_dry_run_is_local_and_never_contacts_cluster(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [])

    def test_foreign_same_name_blocks_every_write(self):
        self.state.write_text(
            json.dumps(
                {
                    "items": [
                        {
                            "kind": "Service",
                            "metadata": {
                                "name": PROJECT,
                                "namespace": "prod",
                                "labels": {"app": "somebody-else"},
                            },
                        }
                    ]
                }
            )
        )
        result = self.run_script(dry=False)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assert_no_writes()

    def test_cross_namespace_collision_blocks_every_write(self):
        self.state.write_text(
            json.dumps(
                {
                    "items": [
                        {
                            "kind": "Deployment",
                            "metadata": {
                                "name": PROJECT + "-api",
                                "namespace": "other",
                                "labels": {"app": PROJECT},
                            },
                        }
                    ]
                }
            )
        )
        self.assertNotEqual(self.run_script(dry=False).returncode, 0)
        self.assert_no_writes()

    def test_foreign_secret_blocks_every_write(self):
        self.state.write_text(
            json.dumps(
                {
                    "items": [],
                    "secret": {
                        "metadata": {
                            "name": PROJECT + "-runtime",
                            "labels": {"app": "other"},
                        }
                    },
                }
            )
        )
        self.assertNotEqual(self.run_script(dry=False).returncode, 0)
        self.assert_no_writes()

    def test_missing_registry_pull_secret_blocks_before_writes(self):
        self.state.write_text(json.dumps({"items": [], "missing_registry": True}))
        result = self.run_script(dry=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("registry pull Secret is missing", result.stderr)
        self.assert_no_writes()

    def test_duplicate_ingress_blocks_before_apply(self):
        self.state.write_text(
            json.dumps(
                {
                    "items": [],
                    "ingress": {
                        "metadata": {"resourceVersion": "1"},
                        "spec": {
                            "rules": [
                                {
                                    "host": self.env["INGRESS_HOST"],
                                    "http": {
                                        "paths": [
                                            {
                                                "backend": {
                                                    "service": {
                                                        "name": "other",
                                                        "port": {"number": 80},
                                                    }
                                                }
                                            }
                                        ]
                                    },
                                }
                            ]
                        },
                    },
                }
            )
        )
        self.assertNotEqual(self.run_script(dry=False).returncode, 0)
        self.assert_no_writes()

    def test_host_collision_on_other_namespace_shared_ingress_blocks_writes(self):
        self.state.write_text(
            json.dumps(
                {
                    "items": [
                        {
                            "kind": "Ingress",
                            "metadata": {"name": "prod-https", "namespace": "other"},
                            "spec": {"rules": [{"host": self.env["INGRESS_HOST"]}]},
                        }
                    ]
                }
            )
        )
        self.assertNotEqual(self.run_script(dry=False).returncode, 0)
        self.assert_no_writes()

    def test_foreign_image_under_project_label_blocks_writes(self):
        self.state.write_text(
            json.dumps(
                {
                    "items": [
                        {
                            "kind": "Deployment",
                            "metadata": {
                                "name": PROJECT + "-api",
                                "namespace": "prod",
                                "labels": {"app": PROJECT},
                            },
                            "spec": {
                                "template": {
                                    "spec": {
                                        "containers": [
                                            {"image": "other-project:latest"}
                                        ]
                                    }
                                }
                            },
                        }
                    ]
                }
            )
        )
        self.assertNotEqual(self.run_script(dry=False).returncode, 0)
        self.assert_no_writes()

    def test_wrong_image_revision_or_runtime_rejection_blocks_cluster_calls(self):
        for extra in ({"IMAGE_REVISION": "wrong"}, {"RUNTIME_CHECK_EXIT": "2"}):
            result = self.run_script(dry=False, **extra)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(self.calls(), [])

    def test_preflight_only_never_writes(self):
        result = self.run_script(dry=False, PREFLIGHT_ONLY="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.calls())
        self.assert_no_writes()

    def test_config_change_produces_new_valid_job_name(self):
        first = self.run_script()
        self.values["SECRET_KEY"] = "another-private-value"
        second = self.run_script()

        def job(result):
            self.assertEqual(result.returncode, 0, result.stderr)
            return next(
                line.split("=", 1)[1]
                for line in result.stdout.splitlines()
                if line.startswith("migration_job=")
            )

        self.assertNotEqual(job(first), job(second))
        self.assertLessEqual(len(job(first)), 63)
        self.assertNotIn("another-private-value", second.stdout + second.stderr)

    def test_apply_checks_everything_before_writes_and_uses_context(self):
        result = self.run_script(dry=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertTrue(calls)
        for call in calls:
            self.assertIn("--context", call["args"])
            self.assertIn("fixture-context", call["args"])
        first_write = next(i for i, c in enumerate(calls) if "apply" in c["args"])
        self.assertTrue(any("-A" in c["args"] for c in calls[:first_write]))
        self.assertTrue(any("secret" in c["args"] for c in calls[:first_write]))
        self.assertEqual(sum("rollout" in c["args"] for c in calls), 9)
        secret_calls = [
            c
            for c in calls
            if "apply" in c["args"] and '"kind": "Secret"' in c["payload"]
        ]
        self.assertEqual(len(secret_calls), 2)
        for call in secret_calls:
            self.assertEqual(
                json.loads(call["payload"])["metadata"]["labels"]["app"], PROJECT
            )
        self.assertNotIn("fixture-secret", result.stdout + result.stderr)

    def test_ingress_append_has_optimistic_lock_and_preserves_other_rules(self):
        result = self.run_script("patch-public-ingress.sh", dry=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        call = next(c for c in self.calls() if "patch" in c["args"])
        patch = json.loads(call["args"][call["args"].index("--patch") + 1])
        self.assertEqual(
            patch[0], {"op": "test", "path": "/metadata/resourceVersion", "value": "1"}
        )
        self.assertIn("prod-https", call["args"])
        self.assertEqual(patch[1]["op"], "add")
        self.assertEqual(patch[1]["path"], "/spec/rules/-")
        self.assertEqual(
            json.loads(self.state.read_text())["ingress"]["spec"]["rules"][0]["host"],
            "other.jetmobo.com",
        )


if __name__ == "__main__":
    unittest.main()
