"""Offline Kubernetes schema and application deployment contract validation."""

from pathlib import Path

import yaml
from kubernetes_validate import validate

root = Path(__file__).resolve().parents[1] / "deploy/k8s"
objects = []
for path in sorted(root.glob("*.yaml")):
    rendered = (
        path.read_text()
        .replace(
            "__APP_IMAGE__",
            "junbo-image.tencentcloudcr.com/prod/tiktok-ad-automation:" + "0" * 40,
        )
        .replace("__RELEASE_ID__", "0" * 40)
        .replace("__CONFIG_DIGEST__", "0" * 64)
        .replace(
            "__JOB_NAME__",
            "tiktok-ad-automation-migration-" + "0" * 12 + "-" + "0" * 12,
        )
    )
    for resource in yaml.safe_load_all(rendered):
        validate(resource, desired_version="1.34.0", strict=True)
        assert len(resource["metadata"]["name"]) <= 63
        assert resource["metadata"]["labels"]["app"] == "tiktok-ad-automation"
        objects.append(resource)
workloads = [o for o in objects if o["kind"] == "Deployment"]
assert len(workloads) == 11
for workload in workloads:
    spec = workload["spec"]["template"]["spec"]
    assert spec["imagePullSecrets"] == [{"name": "tiktok-ad-automation-registry"}]
    container = spec["containers"][0]
    role = workload["metadata"]["labels"]["role"]
    env = {item["name"]: item.get("value") for item in container.get("env", [])}
    if role.startswith("worker-"):
        assert env.get("DATABASE_POOL_SIZE") == "2"
        assert env.get("DATABASE_MAX_OVERFLOW") == "1"
    else:
        assert "DATABASE_POOL_SIZE" not in env and "DATABASE_MAX_OVERFLOW" not in env
    assert all(
        k in container["resources"][budget]
        for budget in ("requests", "limits")
        for k in ("cpu", "memory")
    )
    registration = next(
        m for m in container["volumeMounts"] if m["name"] == "mcp-registration"
    )
    assert registration["subPath"] == "mcp-client.json", (
        "MCP O_NOFOLLOW requires a regular-file subPath mount"
    )
    if workload["metadata"]["labels"]["role"] == "beat":
        assert (
            workload["spec"]["replicas"] == 1
            and workload["spec"]["strategy"]["type"] == "Recreate"
        )
    else:
        assert "readinessProbe" in container
expected_workers = {
    "worker-ads-reporting": ("ads-reporting", 12, "4Gi", "6Gi"),
    "worker-ads-backfill": ("ads-backfill", 1, "512Mi", "1Gi"),
    "worker-ads-live-directory": ("ads-live-directory,ads-directory", 10, "2Gi", "4Gi"),
    "worker-ads-directory": ("ads-directory,ads-publish", 1, "1Gi", "2Gi"),
    "worker-ad-management": ("ad-management", 1, "1Gi", "2Gi"),
}
for workload in workloads:
    role = workload["metadata"]["labels"]["role"]
    if role not in expected_workers:
        continue
    queue, concurrency, request, limit = expected_workers[role]
    container = workload["spec"]["template"]["spec"]["containers"][0]
    command = container["command"]
    assert command[command.index("-Q") + 1] == queue
    assert command[command.index("--pool") + 1] == "prefork"
    assert command[command.index("--concurrency") + 1] == str(concurrency)
    assert (
        command[command.index("--hostname") + 1] == role.removeprefix("worker-") + "@%h"
    )
    assert container["resources"]["requests"]["memory"] == request
    assert container["resources"]["limits"]["memory"] == limit
    if role == "worker-ads-reporting":
        assert container["resources"]["requests"]["cpu"] == "2"
        assert container["resources"]["limits"]["cpu"] == "4"
for name in ("compose.yml", "compose.production.yml"):
    services = yaml.safe_load((root.parents[1] / name).read_text())["services"]
    for role, service in services.items():
        if role.startswith("worker"):
            assert service["environment"]["DATABASE_POOL_SIZE"] == "2"
            assert service["environment"]["DATABASE_MAX_OVERFLOW"] == "1"
    for role, queue, concurrency in (
        ("worker-ads-backfill", "ads-backfill", 1),
        ("worker-ads-live-directory", "ads-live-directory,ads-directory", 10),
        ("worker-ads-reporting", "ads-reporting", 12),
    ):
        command = services[role]["command"]
        assert command[command.index("-Q") + 1] == queue
        assert "--pool=prefork" in command
        assert command[command.index("--concurrency") + 1].endswith(
            ":-" + str(concurrency) + "}"
        )
    if name == "compose.production.yml":
        assert services["worker-ads-reporting"]["cpus"] == 4
        assert services["worker-ads-reporting"]["mem_limit"] == "6g"
assert "--concurrency 12" in (root.parent / "staging-ads-reporting.service").read_text()
for path in root.parent.glob("staging-*.service"):
    content = path.read_text()
    if " worker " in content:
        assert (
            "ExecStart=/usr/bin/env DATABASE_POOL_SIZE=2 DATABASE_MAX_OVERFLOW=1 "
            in content
        )
api = next(o for o in workloads if o["metadata"]["labels"]["role"] == "api")["spec"][
    "template"
]["spec"]["containers"][0]
service = next(o for o in objects if o["kind"] == "Service")
assert api["command"][api["command"].index("--port") + 1] == "80"
assert (
    api["ports"][0]["containerPort"]
    == service["spec"]["ports"][0]["targetPort"]
    == service["spec"]["ports"][0]["port"]
    == 80
)
print(
    f"PASS: {len(objects)} Kubernetes objects, schema 1.34, ports, mounts, resource budgets and Beat update policy"
)
