"""Boot the actual K8s role commands in isolated Docker containers; no external access.

Run: uv run python scripts/check-local-deployment.py --image tiktok-ad-automation:deploy-check
Only containers/networks created by this invocation are removed on exit.
"""

import argparse
import base64
import json
import os
import secrets
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def docker(*args, check=True, timeout=180):
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=False, timeout=timeout
    )
    if check and result.returncode:
        # 本地临时配置也是私密材料，不在错误中输出命令或完整环境。
        raise RuntimeError(f"docker {args[0]} failed: {result.stderr[-1500:]}")
    return result


def retry(check, label, seconds=90):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            print("PASS:", label, flush=True)
            return
        time.sleep(2)
    raise RuntimeError(f"timed out: {label}")


def run(image):
    prefix = "tk-ada-check-" + uuid.uuid4().hex[:10]
    network = prefix + "-net"
    containers = []
    workloads = list(
        yaml.safe_load_all((ROOT / "deploy/k8s/tk-ada-workloads.yaml").read_text())
    )
    profile = json.loads(
        (ROOT / "backend/app/integrations/tiktok/mcp/protocol-profile.json").read_text()
    )
    (ROOT / ".runtime").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="tk-ada-local-check-", dir=ROOT / ".runtime"
    ) as directory:
        private = Path(directory)
        # 私有网络不提供外网出口，不挂载任何现有业务配置或数据卷。
        docker("network", "create", "--internal", network)
        try:
            password = secrets.token_urlsafe(24)
            origin = "https://tiktok-ad-automation-test.jetmobo.com"
            policies = {
                "base": {
                    "app_max_inflight": 1,
                    "endpoint_max_inflight": 1,
                    "tenant_max_inflight": 1,
                    "advertiser_max_inflight": 1,
                    "app_calls_per_window": 1,
                    "endpoint_calls_per_window": 1,
                    "window_ms": 1000,
                    "lease_ms": 970000,
                }
            }
            values = {
                "PROJECT_NAME": "TK-ADA local container validation",
                "SECRET_KEY": secrets.token_urlsafe(32),
                "FIRST_SUPERUSER": "admin",
                "FIRST_SUPERUSER_PASSWORD": password,
                "DATABASE_URL": f"postgresql://postgres:{password}@{prefix}-pg:5432/tiktok_deployment_test",
                "REDIS_URL": f"redis://{prefix}-redis:6379/0",
                "CONNECTION_ENCRYPTION_KEY": base64.urlsafe_b64encode(
                    os.urandom(32)
                ).decode(),
                "FRONTEND_HOST": origin,
                "OBJECT_STORAGE_PROVIDER": "r2",
                "S3_REGION": "auto",
                "S3_ENDPOINT_URL": "https://fixture.r2.cloudflarestorage.com",
                "S3_BUCKET": "local-check",
                "S3_ACCESS_KEY_ID": "synthetic-local-only",
                "S3_SECRET_ACCESS_KEY": "synthetic-local-only",
                "TIKTOK_CALL_POLICIES": json.dumps(policies),
                "ADS_SYNC_ENABLED": "false",
                "AUTOMATED_RULES_ENABLED": "false",
                "MATERIAL_INGEST_ENABLED": "false",
                "MATERIAL_CLEANUP_ENABLED": "false",
                "MCP_REDIRECT_URI": origin + "/api/integrations/tiktok/mcp/callback",
                "MCP_CLIENT_REGISTRATION_REF": "/run/tk-ada/mcp-client.json",
            }
            envfile = private / "runtime.env"
            envfile.write_text(
                "\n".join(f"{key}={value}" for key, value in values.items())
            )
            envfile.chmod(0o600)
            registration = private / "mcp.json"
            registration.write_text(
                json.dumps(
                    {
                        "client_id": "synthetic-local-only",
                        "issuer": profile["issuer"],
                        "resource": profile["resource"],
                        "token_endpoint_auth_method": "none",
                        "redirect_uris": [values["MCP_REDIRECT_URI"]],
                    }
                )
            )
            registration.chmod(0o600)
            common = [
                "--network",
                network,
                "--env-file",
                str(envfile),
                "--mount",
                f"type=bind,source={registration},target=/run/tk-ada/mcp-client.json,readonly",
            ]

            def start(name, image_name, args, options=()):
                full_name = prefix + "-" + name
                docker("run", "-d", "--name", full_name, *options, image_name, *args)
                containers.append(full_name)
                return full_name

            pg = start(
                "pg",
                "postgres:18",
                [],
                [
                    "--network",
                    network,
                    "-e",
                    f"POSTGRES_PASSWORD={password}",
                    "-e",
                    "POSTGRES_DB=tiktok_deployment_test",
                ],
            )
            redis = start(
                "redis",
                "redis:8",
                ["redis-server", "--save", "", "--appendonly", "no"],
                ["--network", network],
            )
            retry(
                lambda: (
                    docker(
                        "exec", pg, "pg_isready", "-U", "postgres", check=False
                    ).returncode
                    == 0
                ),
                "isolated PostgreSQL ready",
            )
            retry(
                lambda: (
                    docker("exec", redis, "redis-cli", "ping", check=False).returncode
                    == 0
                ),
                "isolated Redis ready",
            )
            print(
                docker(
                    "run",
                    "--rm",
                    *common,
                    image,
                    "python",
                    "scripts/check_deployment.py",
                ).stdout,
                flush=True,
            )
            # 错误额度必须被实际应用校验器拒绝。
            invalid = docker(
                "run",
                "--rm",
                *common,
                "-e",
                "TIKTOK_CALL_POLICIES={}",
                image,
                "python",
                "scripts/check_deployment.py",
                check=False,
            )
            assert invalid.returncode != 0, "empty call policies incorrectly accepted"
            print("PASS: invalid runtime policy rejected", flush=True)
            docker(
                "run",
                "--rm",
                *common,
                image,
                "bash",
                "scripts/prestart.sh",
                timeout=300,
            )
            print("PASS: fresh local initialization", flush=True)
            api_name = None
            worker_names = []
            for workload in workloads:
                role = workload["metadata"]["labels"]["role"]
                container = workload["spec"]["template"]["spec"]["containers"][0]
                options = [
                    *common,
                    "--memory",
                    container["resources"]["limits"]["memory"]
                    .replace("Gi", "g")
                    .replace("Mi", "m"),
                    "--cpus",
                    container["resources"]["limits"]["cpu"],
                ]
                for variable in container.get("env", []):
                    options += ["-e", variable["name"] + "=" + variable["value"]]
                if role == "beat":
                    options += ["--tmpfs", "/var/run/celery"]
                name = start(role, image, container["command"], options)
                if role == "api":
                    api_name = name
                elif role.startswith("worker-"):
                    worker_names.append(name)
            assert api_name
            retry(
                lambda: (
                    docker(
                        "exec",
                        api_name,
                        "python",
                        "-c",
                        'import urllib.request; urllib.request.urlopen("http://127.0.0.1:80/api/utils/health-check/", timeout=3)',
                        check=False,
                    ).returncode
                    == 0
                ),
                "API listens on port 80",
            )
            probe = """import json, urllib.request, urllib.parse, urllib.error, os
base='http://127.0.0.1:80'
for path in ('/', '/login'):
 r=urllib.request.urlopen(urllib.request.Request(base+path,headers={'Accept':'text/html'})); assert r.status == 200 and 'text/html' in r.headers['Content-Type']
try: urllib.request.urlopen(base+'/api/local-check-missing')
except urllib.error.HTTPError as e: assert e.code == 404
else: raise AssertionError('API fell through to SPA')
body=urllib.parse.urlencode({'username':os.environ['FIRST_SUPERUSER'],'password':os.environ['FIRST_SUPERUSER_PASSWORD']}).encode()
token=json.load(urllib.request.urlopen(base+'/api/login/access-token',data=body))['access_token']
request=urllib.request.Request(base+'/api/users/me', headers={'Authorization':'Bearer '+token})
assert json.load(urllib.request.urlopen(request))['username'] == 'admin'
print('PASS: built frontend, login token, authenticated user, API boundary')
"""
            print(docker("exec", api_name, "python", "-c", probe).stdout, flush=True)
            for workload in workloads:
                role = workload["metadata"]["labels"]["role"]
                if role.startswith("worker-"):
                    command = workload["spec"]["template"]["spec"]["containers"][0][
                        "readinessProbe"
                    ]["exec"]["command"]
                    name = prefix + "-" + role
                    retry(
                        lambda name=name, command=command: (
                            docker("exec", name, *command, check=False).returncode == 0
                        ),
                        role + " readiness",
                        seconds=120,
                    )
                pool_check = "from app.core.config import settings; " + (
                    "assert (settings.DATABASE_POOL_SIZE, settings.DATABASE_MAX_OVERFLOW) == (2, 1)"
                    if role.startswith("worker-")
                    else "assert (settings.DATABASE_POOL_SIZE, settings.DATABASE_MAX_OVERFLOW) == (5, 10)"
                )
                docker("exec", prefix + "-" + role, "python", "-c", pool_check)
            for name in containers:
                state = json.loads(
                    docker("inspect", "--format", "{{json .State}}", name).stdout
                )
                assert state["Running"] and not state["OOMKilled"], (
                    name + " is not healthy"
                )
            docker("restart", api_name)
            retry(
                lambda: (
                    docker(
                        "exec",
                        api_name,
                        "python",
                        "-c",
                        'import urllib.request; urllib.request.urlopen("http://127.0.0.1:80/api/utils/health-check/", timeout=3)',
                        check=False,
                    ).returncode
                    == 0
                ),
                "API restart recovery",
            )
            print(
                docker(
                    "stats",
                    "--no-stream",
                    "--format",
                    "{{.Name}} {{.MemUsage}}",
                    *containers,
                ).stdout,
                flush=True,
            )
            print(
                "PASS: all eleven application roles started using K8s commands; nine Worker probes passed; no external integration exercised",
                flush=True,
            )
        finally:
            # 只清理本轮唯一前缀下实际创建的容器，永不使用 prune/down -v。
            for name in reversed(containers):
                docker("rm", "-f", "-v", name, check=False)
            docker("network", "rm", network, check=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    run(args.image)
