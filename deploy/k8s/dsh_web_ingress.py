"""生成独立 DSH 路径的原子 JSON Patch；保留共享 ALB 的所有现有路由。"""

import argparse
import json
import subprocess
import sys

HOST = "tkada.shortclipflix.com"


def ingress_patch(ingress):
    rules = ingress["spec"]["rules"]
    matches = [index for index, rule in enumerate(rules) if rule.get("host") == HOST]
    if len(matches) != 1:
        raise ValueError("Expected exactly one existing TK-ADA host rule")
    index = matches[0]
    paths = rules[index]["http"]["paths"]
    desired = {
        "path": "/dsh", "pathType": "Prefix",
        "backend": {"service": {"name": "tiktok-ad-automation-dsh", "port": {"number": 9081}}},
    }
    existing = [path for path in paths if path["path"].rstrip("/") == "/dsh"]
    if existing:
        if existing == [desired]:
            return []
        raise ValueError("Conflicting DSH ingress route; review manually")
    # 更具体的路径排在 / 前；resourceVersion test 防止覆盖并发发布。
    return [
        {"op": "test", "path": "/metadata/resourceVersion", "value": ingress["metadata"]["resourceVersion"]},
        {"op": "add", "path": f"/spec/rules/{index}/http/paths/0", "value": desired},
    ]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--kubectl", default="kubectl")
    parser.add_argument("--apply", action="store_true", help="显式应用；默认只输出待审查补丁")
    args = parser.parse_args()
    command = [args.kubectl, "--kubeconfig", args.kubeconfig, "-n", "test"]
    resource = json.loads(subprocess.check_output(command + ["get", "ingress", "test-https", "-o", "json"]))
    patch = ingress_patch(resource)
    if args.apply and patch:
        subprocess.run(command + ["patch", "ingress", "test-https", "--type=json", "-p", json.dumps(patch)], check=True)
    else:
        sys.stdout.write(json.dumps(patch, indent=2) + "\n")
