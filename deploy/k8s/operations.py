"""TK-ADA 部署门禁与编排；只使用标准库，不加载项目私有 .env。"""

import base64
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

PROJECT = "tiktok-ad-automation"
IMAGE_PREFIX = (
    f"sg-acr-prod-registry-vpc.ap-southeast-1.cr.aliyuncs.com/test/{PROJECT}:"
)
PUBLIC_IMAGE_PREFIX = (
    "sg-acr-prod-registry.ap-southeast-1.cr.aliyuncs.com/test/" + PROJECT + ":"
)
DIRECTORY = Path(__file__).resolve().parent
ROLES = (
    "api",
    "worker-resource",
    "worker-results",
    "worker-builds",
    "worker-control",
    "worker-ads-directory",
    "worker-ads-reporting",
    "worker-ad-management",
    "beat",
)
FLAGS = ("MATERIAL_INGEST_ENABLED", "MATERIAL_CLEANUP_ENABLED", "ADS_SYNC_ENABLED")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def setting(name):
    value = os.environ.get(name, "")
    require(value.strip(), f"{name} must be explicitly supplied")
    return value


def read_env(path):
    values = {}
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        require(
            separator and re.fullmatch(r"[A-Z][A-Z0-9_]*", key),
            f"invalid env key on line {number}",
        )
        require(key not in values, f"duplicate env key: {key}")
        # kubectl env-file 不解释 shell 引号或变量，避免把引号当成真实凭据。
        require(
            not value.startswith(('"', "'")), f"{key} must use unquoted env-file syntax"
        )
        values[key] = value
    return values


class Deployment:
    def __init__(self):
        self.namespace = setting("K8S_NAMESPACE")
        require(
            self.namespace == "test",
            "shared test-https Ingress requires K8S_NAMESPACE=test",
        )
        self.context = setting("K8S_CONTEXT")
        self.host = setting("INGRESS_HOST")
        require(
            self.host == "tkada.shortclipflix.com"
            or re.fullmatch(
                r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.jetmobo\.com", self.host
            ),
            "INGRESS_HOST must be tkada.shortclipflix.com or an assigned single-level jetmobo.com host",
        )
        self.base = [
            "kubectl",
            "--context",
            self.context,
            "-n",
            self.namespace,
        ]

    def kubectl(self, *args, payload=None):
        timeout = "20m" if args[0] in ("wait", "rollout") else "30s"
        result = subprocess.run(
            self.base + [f"--request-timeout={timeout}"] + list(args),
            input=payload,
            capture_output=True,
            text=True,
            check=False,
        )
        # apply 的错误可能包含 Secret；只报告动作和退出码，不回显正文/私密配置。
        require(
            result.returncode == 0,
            f"kubectl {args[0]} failed (exit {result.returncode}); inspect target resources privately",
        )
        return result.stdout.strip()

    def get(self, kind, name):
        output = self.kubectl("get", kind, name, "--ignore-not-found", "-o", "json")
        return json.loads(output) if output else None

    def owner(self, obj):
        metadata = obj.get("metadata", {})
        name = metadata.get("name", "")
        require(
            metadata.get("labels", {}).get("app") == PROJECT,
            f"ownership conflict: {name}",
        )
        containers = (
            obj.get("spec", {})
            .get("template", {})
            .get("spec", {})
            .get("containers", [])
        )
        for container in containers:
            require(
                re.fullmatch(
                    re.escape(IMAGE_PREFIX) + r"[0-9a-f]{40}",
                    container.get("image", ""),
                ),
                f"foreign image in existing resource: {name}",
            )

    def ingress(self, allow_existing=False):
        ingress = self.get("ingress", "test-https")
        require(ingress is not None, "shared test-https Ingress not found")
        # 自定义生产域名不能借用 jetmobo 的证书；只核对入口声明，不读取共享 Secret。
        tls_hosts = [
            host
            for entry in ingress.get("spec", {}).get("tls", [])
            for host in entry.get("hosts", [])
        ]
        require(
            any(
                host == self.host
                or (host.startswith("*.") and self.host.split(".", 1)[1] == host[2:])
                for host in tls_hosts
            ),
            "shared Ingress TLS hosts do not cover INGRESS_HOST; configure its certificate before deployment",
        )
        rules = ingress.get("spec", {}).get("rules", [])
        require(
            rules,
            "shared test-https Ingress has no rules; inspect with operations team",
        )
        for rule in rules:
            if rule.get("host") == self.host:
                paths = rule.get("http", {}).get("paths", [])
                own = (
                    len(paths) == 1
                    and paths[0].get("path") == "/"
                    and paths[0].get("backend", {}).get("service")
                    == {"name": PROJECT, "port": {"number": 80}}
                )
                require(
                    allow_existing and own,
                    f"host already exists or belongs to another service: {self.host}",
                )
        return ingress

    def validate_runtime(self):
        self.release = setting("RELEASE_ID")
        require(
            re.fullmatch(r"[0-9a-f]{40}", self.release),
            "RELEASE_ID must be a 40-character Git SHA",
        )
        self.image = setting("APP_IMAGE")
        require(
            self.image == IMAGE_PREFIX + self.release,
            "APP_IMAGE must match the company project and RELEASE_ID",
        )
        self.values = read_env(Path(setting("RUNTIME_ENV_FILE")))
        required = (
            "SECRET_KEY",
            "FIRST_SUPERUSER",
            "FIRST_SUPERUSER_PASSWORD",
            "DATABASE_URL",
            "REDIS_URL",
            "CONNECTION_ENCRYPTION_KEY",
            "S3_ENDPOINT_URL",
            "S3_BUCKET",
            "S3_ACCESS_KEY_ID",
            "S3_SECRET_ACCESS_KEY",
            "TIKTOK_CALL_POLICIES",
            "FRONTEND_HOST",
            "MCP_REDIRECT_URI",
        )
        for key in required:
            value = self.values.get(key, "")
            require(
                value.strip()
                and "<" not in value
                and ">" not in value
                and value != "changethis",
                f"{key} is missing or a placeholder",
            )
        for key in FLAGS:
            require(
                self.values.get(key) in ("true", "false"),
                f"{key} must explicitly be true or false",
            )
        for key in ("DATABASE_URL", "REDIS_URL"):
            host = urlsplit(self.values[key]).hostname
            require(
                host and host not in ("localhost", "host.docker.internal"),
                f"{key} must use a cluster-reachable endpoint",
            )
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                address = None
            require(
                address is None or not (address.is_loopback or address.is_unspecified),
                f"{key} must not use loopback",
            )
        origin = f"https://{self.host}"
        require(
            self.values["FRONTEND_HOST"] == origin,
            "FRONTEND_HOST must match INGRESS_HOST over HTTPS",
        )
        require(
            self.values["MCP_REDIRECT_URI"]
            == origin + "/api/integrations/tiktok/mcp/callback",
            "MCP_REDIRECT_URI must match the target origin and callback path",
        )
        api_keys = ("TIKTOK_APP_ID", "TIKTOK_APP_SECRET", "TIKTOK_REDIRECT_URI")
        if any(self.values.get(key) for key in api_keys):
            require(
                all(self.values.get(key) for key in api_keys),
                "TikTok API registration is incomplete",
            )
            require(
                self.values["TIKTOK_REDIRECT_URI"]
                == origin + "/api/integrations/tiktok/callback",
                "TIKTOK_REDIRECT_URI must match the target origin and callback path",
            )
        policies = json.loads(self.values["TIKTOK_CALL_POLICIES"])
        require(
            isinstance(policies, dict) and policies,
            "TIKTOK_CALL_POLICIES must be a non-empty object",
        )
        self.registration = Path(setting("MCP_CLIENT_REGISTRATION_FILE")).read_bytes()
        registration = json.loads(self.registration)
        require(
            isinstance(registration, dict) and registration.get("client_id"),
            "MCP registration must contain client_id",
        )
        if registration.get("redirect_uris"):
            require(
                self.values["MCP_REDIRECT_URI"] in registration["redirect_uris"],
                "MCP registration does not allow the target callback",
            )
        # 配置变化也触发所有角色重建；使用摘要，不把密钥放入 Pod 注解。
        self.digest = hashlib.sha256(
            json.dumps(self.values, sort_keys=True).encode() + self.registration
        ).hexdigest()
        # Job 名称必须 <=63 字符；原完整 SHA 名称会超过 Kubernetes 限制。
        self.job_name = f"{PROJECT}-migration-{self.release[:12]}-{self.digest[:12]}"

    def validate_image(self):
        # 本机从公网 ACR 拉取；Pod 使用同仓库同 SHA 的 VPC 地址。
        local_image = PUBLIC_IMAGE_PREFIX + self.release
        # 使用已拉取镜像验证真实配置，禁止校验容器访问网络或现有业务队列。
        result = subprocess.run(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                '{{index .Config.Labels "org.opencontainers.image.revision"}}',
                local_image,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        require(
            result.returncode == 0 and result.stdout.strip() == self.release,
            "local release image is missing or its revision differs; pull and verify the exact release image",
        )
        registration = Path(setting("MCP_CLIENT_REGISTRATION_FILE")).resolve()
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--pull=never",
                "--network",
                "none",
                "--env-file",
                str(Path(setting("RUNTIME_ENV_FILE")).resolve()),
                "--mount",
                f"type=bind,source={registration},target=/run/tk-ada/mcp-client.json,readonly",
                "-e",
                "MCP_CLIENT_REGISTRATION_REF=/run/tk-ada/mcp-client.json",
                local_image,
                "python",
                "scripts/check_deployment.py",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        require(
            result.returncode == 0,
            "offline image runtime validation failed; run scripts/check_deployment.py privately for diagnostic category",
        )

    def preflight(self):
        self.kubectl("get", "namespace", self.namespace, "-o", "json")
        # 全集群查重；Secret 仅查询本项目运行配置、注册文件和镜像拉取凭据。
        inventory = json.loads(
            self.kubectl(
                "get",
                "deployments,services,ingresses,configmaps,persistentvolumeclaims",
                "-A",
                "-o",
                "json",
            )
        )
        retired = self.retired_source(inventory["items"])
        for obj in inventory["items"]:
            metadata = obj.get("metadata", {})
            name = metadata.get("name", "")
            labeled = metadata.get("labels", {}).get("app") == PROJECT
            if name == PROJECT or name.startswith(PROJECT + "-") or labeled:
                if (obj.get("kind"), metadata.get("namespace"), name) in retired:
                    continue
                require(
                    metadata.get("namespace") == self.namespace,
                    f"cross-namespace project collision: {name}",
                )
                self.owner(obj)
            if obj.get("kind") == "Ingress" and (
                name != "test-https" or metadata.get("namespace") != self.namespace
            ):
                require(
                    all(
                        rule.get("host") != self.host
                        for rule in obj.get("spec", {}).get("rules", [])
                    ),
                    "host collision on another Ingress",
                )
        for suffix in ("runtime", "mcp-registration"):
            obj = self.get("secret", f"{PROJECT}-{suffix}")
            if obj:
                self.owner(obj)
        registry = self.get("secret", f"{PROJECT}-registry")
        require(registry is not None, "project registry pull Secret is missing")
        self.owner(registry)
        require(
            registry.get("type") == "kubernetes.io/dockerconfigjson"
            and registry.get("data", {}).get(".dockerconfigjson"),
            "project registry pull Secret has invalid format",
        )
        self.ingress(allow_existing=True)
        self.existing_job = self.get("job", self.job_name)
        if self.existing_job:
            self.owner(self.existing_job)
            annotations = self.existing_job.get("metadata", {}).get("annotations", {})
            require(
                annotations.get("tk-ada/release") == self.release
                and annotations.get("tk-ada/config-digest") == self.digest,
                "existing migration Job release/config mismatch",
            )
            require(
                any(
                    c.get("type") == "Complete" and c.get("status") == "True"
                    for c in self.existing_job.get("status", {}).get("conditions", [])
                ),
                "existing migration Job is not complete",
            )

    def retired_source(self, inventory):
        # 命名空间迁移可保留停用资源用于回退；只豁免显式归档中 UID 相同的资源。
        # 不接受仅靠 namespace 或 app 标签放行，旧消费者仍在运行时必须阻断。
        path = os.environ.get("RETIRED_SOURCE_INVENTORY")
        if not path:
            return set()
        archive = json.loads(Path(path).read_text())
        namespace = archive["namespace"]
        require(namespace != self.namespace, "retired source must differ from target")
        expected = {}
        for obj in archive["items"]:
            metadata = obj["metadata"]
            require(
                metadata.get("namespace") == namespace
                and metadata.get("labels", {}).get("app") == PROJECT
                and metadata.get("uid")
                and (
                    metadata["name"] == PROJECT
                    or metadata["name"].startswith(PROJECT + "-")
                )
                and obj["kind"]
                in ("Deployment", "Service", "ConfigMap", "PersistentVolumeClaim"),
                "invalid retired source inventory",
            )
            expected[(obj["kind"], namespace, metadata["name"])] = obj
        require(expected, "retired source inventory must not be empty")
        found = set()
        for obj in inventory:
            meta = obj.get("metadata", {})
            key = (obj.get("kind"), meta.get("namespace"), meta.get("name"))
            if key not in expected:
                continue
            original = expected[key]
            require(
                meta.get("uid") == original["metadata"]["uid"]
                and meta.get("labels", {}).get("app") == PROJECT,
                "retired source identity changed",
            )
            if obj["kind"] == "Deployment":
                require(
                    obj.get("spec", {}).get("replicas", 1) == 0
                    and not obj.get("status", {}).get("replicas", 0)
                    and obj["spec"]["template"] == original["spec"]["template"],
                    "retired source Deployment is active or changed",
                )
            found.add(key)
        pods = json.loads(
            self.kubectl(
                "get", "pods", "-n", namespace, "-l", f"app={PROJECT}", "-o", "json"
            )
        )
        require(
            not any(
                pod.get("status", {}).get("phase") in ("Pending", "Running", "Unknown")
                and (
                    pod.get("metadata", {}).get("labels", {}).get("role") in ROLES
                    or any(
                        ref.get("kind") == "ReplicaSet"
                        for ref in pod.get("metadata", {}).get("ownerReferences", [])
                    )
                )
                for pod in pods["items"]
            ),
            "retired source still has application Pods",
        )
        return found

    def render(self, name):
        return (
            (DIRECTORY / name)
            .read_text()
            .replace("__APP_IMAGE__", self.image)
            .replace("__RELEASE_ID__", self.release)
            .replace("__JOB_NAME__", self.job_name)
            .replace("__CONFIG_DIGEST__", self.digest)
        )

    def apply_secret(self, suffix, values):
        secret = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": f"{PROJECT}-{suffix}",
                "namespace": self.namespace,
                "labels": {"app": PROJECT},
            },
            "type": "Opaque",
            "data": {k: base64.b64encode(v).decode() for k, v in values.items()},
        }
        # server-side apply 不将 Secret 再复制进 last-applied 注解。
        self.kubectl(
            "apply",
            "--server-side",
            "--field-manager=tk-ada-deploy",
            "-f",
            "-",
            payload=json.dumps(secret),
        )

    def apply(self):
        self.validate_runtime()
        print(
            f"context={self.context}\nnamespace={self.namespace}\nimage={self.image}\nmigration_job={self.job_name}"
        )
        print("flags=" + ",".join(f"{key}={self.values[key]}" for key in FLAGS))
        if os.environ.get("DRY_RUN") == "1":
            print(
                "dry-run: local configuration passed; cluster ownership and rollout not checked"
            )
            return
        self.validate_image()
        self.preflight()
        if os.environ.get("PREFLIGHT_ONLY") == "1":
            print("PASS: read-only cluster preflight; no resources written")
            return
        self.apply_secret("runtime", {k: v.encode() for k, v in self.values.items()})
        self.apply_secret("mcp-registration", {"mcp-client.json": self.registration})
        self.kubectl("apply", "-f", "-", payload=self.render("tk-ada.yaml"))
        if not self.existing_job:
            self.kubectl("create", "-f", "-", payload=self.render("migration-job.yaml"))
            self.kubectl(
                "wait",
                "--for=condition=complete",
                "--timeout=15m",
                f"job/{self.job_name}",
            )
        workloads = self.render("tk-ada-workloads.yaml")
        if os.environ.get("RELEASE_HOLD_WORKLOADS") == "1":
            # 发布编排已停写并验证备份；迁移后保持零副本，验收/恢复由编排统一负责。
            require(
                workloads.count("  replicas: 1") == len(ROLES),
                "unexpected workload replica declarations",
            )
            workloads = workloads.replace("  replicas: 1", "  replicas: 0")
        self.kubectl("apply", "-f", "-", payload=workloads)
        if os.environ.get("RELEASE_HOLD_WORKLOADS") == "1":
            print("PASS: migration completed; workloads held at zero replicas")
            return
        for role in ROLES:
            print(f"waiting for deployment/{PROJECT}-{role}", flush=True)
            self.kubectl(
                "rollout", "status", f"deployment/{PROJECT}-{role}", "--timeout=20m"
            )
        print("PASS: rollout completed; HTTPS and business acceptance remain required")

    def patch_ingress(self):
        print(
            f"append_host={self.host}\nservice={PROJECT}\nexisting_rules_are_preserved=true"
        )
        if os.environ.get("DRY_RUN") == "1":
            return
        service = self.get("service", PROJECT)
        require(service is not None, "project Service does not exist")
        self.owner(service)
        require(
            service.get("spec", {}).get("selector") == {"app": PROJECT, "role": "api"},
            "project Service selector mismatch",
        )
        require(
            any(
                p.get("port") == 80 and p.get("targetPort") == 80
                for p in service.get("spec", {}).get("ports", [])
            ),
            "project Service must use port 80",
        )
        ingress = self.ingress()
        version = ingress.get("metadata", {}).get("resourceVersion")
        require(version, "Ingress resourceVersion missing")
        rule = {
            "host": self.host,
            "http": {
                "paths": [
                    {
                        "path": "/",
                        "pathType": "Prefix",
                        "backend": {
                            "service": {"name": PROJECT, "port": {"number": 80}}
                        },
                    }
                ]
            },
        }
        patch = [
            {"op": "test", "path": "/metadata/resourceVersion", "value": version},
            {"op": "add", "path": "/spec/rules/-", "value": rule},
        ]
        self.kubectl(
            "patch",
            "ingress",
            "test-https",
            "--type=json",
            "--patch",
            json.dumps(patch),
        )
        updated = self.get("ingress", "test-https")
        expected = ingress["spec"]["rules"] + [rule]
        require(
            updated["spec"]["rules"] == expected,
            "Ingress readback differs; inspect without overwriting shared rules",
        )
        print(f"readback_host={self.host}")


if __name__ == "__main__":
    try:
        deployment = Deployment()
        if sys.argv[1:] == ["apply"]:
            deployment.apply()
        elif sys.argv[1:] == ["ingress"]:
            deployment.patch_ingress()
        else:
            raise ValueError("usage: operations.py apply|ingress")
    except (ValueError, OSError, KeyError) as error:
        # JSON/URL 解析异常可能含原始私密值，统一隐藏非本程序定义的异常正文。
        message = (
            str(error)
            if type(error) is ValueError and not isinstance(error, json.JSONDecodeError)
            else type(error).__name__
        )
        print(f"[deploy/k8s] {message}", file=sys.stderr)
        sys.exit(2)
