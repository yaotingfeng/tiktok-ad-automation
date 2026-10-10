"""生成明确目标的集群备份资产；默认 suspend，实际验证后再启用。"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "release"))
from common import PROJECT, ROLES

NAME = PROJECT + "-backup"


def resources(image):
    if not re.fullmatch(
        r"sg-acr-prod-registry-vpc\.ap-southeast-1\.cr\.aliyuncs\.com/test/tiktok-ad-automation@sha256:[0-9a-f]{64}",
        image,
    ):
        raise ValueError("Backup image must use an immutable project VPC digest")
    labels = {"app": PROJECT, "role": "backup-controller"}

    def obj(kind, name, api="v1", **extra):
        return {
            "apiVersion": api,
            "kind": kind,
            "metadata": {"name": name, "namespace": "test", "labels": labels},
            **extra,
        }

    assets = [
        {
            "apiVersion": "storage.k8s.io/v1",
            "kind": "StorageClass",
            "metadata": {"name": NAME, "labels": labels},
            "provisioner": "diskplugin.csi.alibabacloud.com",
            "parameters": {"type": "cloud_essd,cloud_ssd,cloud_efficiency"},
            "reclaimPolicy": "Retain",
            "volumeBindingMode": "WaitForFirstConsumer",
            "allowVolumeExpansion": True,
        },
        obj(
            "PersistentVolumeClaim",
            NAME,
            spec={
                "accessModes": ["ReadWriteOnce"],
                "storageClassName": NAME,
                "resources": {"requests": {"storage": "20Gi"}},
            },
        ),
        obj("ServiceAccount", NAME, automountServiceAccountToken=False),
        obj(
            "Role",
            NAME,
            "rbac.authorization.k8s.io/v1",
            rules=[
                {
                    "apiGroups": ["apps"],
                    "resources": ["deployments"],
                    "verbs": ["get", "list"],
                },
                {
                    "apiGroups": ["apps"],
                    "resources": ["deployments/scale"],
                    "resourceNames": [PROJECT + "-" + r for r in ROLES],
                    "verbs": ["get", "patch", "update"],
                },
                {
                    "apiGroups": [""],
                    "resources": ["secrets"],
                    "resourceNames": [
                        PROJECT + "-" + suffix
                        for suffix in ("runtime", "mcp-registration", "registry")
                    ],
                    "verbs": ["get"],
                },
                {
                    "apiGroups": [""],
                    "resources": ["configmaps"],
                    "verbs": ["get", "list", "create"],
                },
                {
                    "apiGroups": [""],
                    "resources": ["configmaps"],
                    "resourceNames": [PROJECT + "-release-lock"],
                    "verbs": ["delete"],
                },
                {
                    "apiGroups": [""],
                    "resources": ["services", "persistentvolumeclaims"],
                    "verbs": ["get", "list"],
                },
                {
                    "apiGroups": [""],
                    "resources": ["pods"],
                    "verbs": ["get", "list", "watch", "create", "delete"],
                },
                {
                    "apiGroups": [""],
                    "resources": ["pods/exec"],
                    "verbs": ["create", "get"],
                },
                {
                    "apiGroups": ["networking.k8s.io"],
                    "resources": ["ingresses"],
                    "resourceNames": ["test-https"],
                    "verbs": ["get"],
                },
            ],
        ),
        obj(
            "RoleBinding",
            NAME,
            "rbac.authorization.k8s.io/v1",
            roleRef={
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "Role",
                "name": NAME,
            },
            subjects=[{"kind": "ServiceAccount", "name": NAME, "namespace": "test"}],
        ),
        obj(
            "NetworkPolicy",
            NAME + "-restore-isolation",
            "networking.k8s.io/v1",
            spec={
                "podSelector": {
                    "matchLabels": {"app": PROJECT, "role": "backup-restore"}
                },
                "policyTypes": ["Ingress", "Egress"],
                "ingress": [],
                "egress": [],
            },
        ),
        obj(
            "CronJob",
            NAME,
            "batch/v1",
            spec={
                "schedule": "0 3 * * *",
                "timeZone": "Asia/Shanghai",
                "suspend": True,
                "concurrencyPolicy": "Forbid",
                "startingDeadlineSeconds": 1800,
                "successfulJobsHistoryLimit": 2,
                "failedJobsHistoryLimit": 3,
                "jobTemplate": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "backoffLimit": 0,
                        "template": {
                            "metadata": {"labels": labels},
                            "spec": {
                                "serviceAccountName": NAME,
                                "automountServiceAccountToken": True,
                                "restartPolicy": "Never",
                                "terminationGracePeriodSeconds": 60,
                                "imagePullSecrets": [{"name": PROJECT + "-registry"}],
                                "affinity": {
                                    "podAffinity": {
                                        "requiredDuringSchedulingIgnoredDuringExecution": [
                                            {
                                                "labelSelector": {
                                                    "matchLabels": {
                                                        "app": PROJECT,
                                                        "role": "beat",
                                                    }
                                                },
                                                "topologyKey": "kubernetes.io/hostname",
                                            }
                                        ]
                                    }
                                },
                                "containers": [
                                    {
                                        "name": "backup",
                                        "image": image,
                                        "args": ["backup"],
                                        "env": [
                                            {
                                                "name": "NODE_NAME",
                                                "valueFrom": {
                                                    "fieldRef": {
                                                        "fieldPath": "spec.nodeName"
                                                    }
                                                },
                                            },
                                            {"name": "BACKUP_KEEP", "value": "14"},
                                        ],
                                        "volumeMounts": [
                                            {
                                                "name": "backups",
                                                "mountPath": "/backups",
                                            },
                                            {
                                                "name": "public",
                                                "mountPath": "/backup-public",
                                                "readOnly": True,
                                            },
                                        ],
                                        "resources": {
                                            "requests": {
                                                "cpu": "50m",
                                                "memory": "256Mi",
                                            },
                                            "limits": {"cpu": "1", "memory": "1Gi"},
                                        },
                                    }
                                ],
                                "volumes": [
                                    {
                                        "name": "backups",
                                        "persistentVolumeClaim": {"claimName": NAME},
                                    },
                                    {
                                        "name": "public",
                                        "configMap": {"name": NAME + "-public"},
                                    },
                                ],
                            },
                        },
                    },
                },
            },
        ),
    ]
    # 退休 prod 消费者只读；不授予旧 namespace 写权限。
    assets.extend(
        [
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "Role",
                "metadata": {
                    "name": NAME + "-retired-read",
                    "namespace": "prod",
                    "labels": labels,
                },
                "rules": [
                    {
                        "apiGroups": ["apps"],
                        "resources": ["deployments"],
                        "resourceNames": [PROJECT + "-" + r for r in ROLES],
                        "verbs": ["get"],
                    }
                ],
            },
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "RoleBinding",
                "metadata": {
                    "name": NAME + "-retired-read",
                    "namespace": "prod",
                    "labels": labels,
                },
                "roleRef": {
                    "apiGroup": "rbac.authorization.k8s.io",
                    "kind": "Role",
                    "name": NAME + "-retired-read",
                },
                "subjects": [
                    {"kind": "ServiceAccount", "name": NAME, "namespace": "test"}
                ],
            },
        ]
    )
    return {"apiVersion": "v1", "kind": "List", "items": assets}


if __name__ == "__main__":
    print(json.dumps(resources(sys.argv[1]), indent=2))
