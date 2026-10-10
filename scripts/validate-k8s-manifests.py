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
assert len(workloads) == 9
for workload in workloads:
    spec = workload["spec"]["template"]["spec"]
    assert spec["imagePullSecrets"] == [{"name": "tiktok-ad-automation-registry"}]
    container = spec["containers"][0]
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
