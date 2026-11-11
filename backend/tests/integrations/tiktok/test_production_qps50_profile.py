"""生产50QPS配置：共用物理额度、保留MCP与上传内存边界。"""

import json
from pathlib import Path

import pytest

from app.core.config import settings
from app.integrations.tiktok.platform_quotas import SDK_ENDPOINTS, sdk_quota
from app.jobs.admission import admission_policy

PROFILE = json.loads(
    (
        Path(__file__).resolve().parents[4] / "deploy/k8s/tiktok-ultimate50.json"
    ).read_text()
)


@pytest.fixture
def _configured(monkeypatch):
    monkeypatch.setattr(settings, "TIKTOK_CALL_POLICIES", PROFILE["policies"])
    return PROFILE


@pytest.mark.parametrize("operation,physical", list(SDK_ENDPOINTS.items()))
def test_all_registered_sdk_calls_use_ultimate_physical_quota(
    _configured, operation, physical
):
    p = admission_policy(operation, channel="OFFICIAL_API", physical_endpoint=physical)
    assert (
        p.app_calls_per_window,
        p.endpoint_calls_per_window,
        p.endpoint_calls_per_minute,
        p.endpoint_calls_per_day,
    ) == (50, *sdk_quota("ULTIMATE", physical))
    assert p.app_max_inflight == 192 and p.tenant_max_inflight == 192
    assert p.endpoint_max_inflight == (
        4 if physical.endswith("/file/video/ad/upload/") else 96
    )


def test_special_regular_ad_create_keeps_its_specific_limits(_configured):
    p = admission_policy(
        "ordinary",
        channel="OFFICIAL_API",
        physical_endpoint="POST /open_api/v1.3/ad/create/",
    )
    assert (
        p.endpoint_calls_per_window,
        p.endpoint_calls_per_minute,
        p.endpoint_calls_per_day,
    ) == (15, 300, 86400)


def test_mcp_does_not_inherit_sdk_fifty_qps(_configured):
    p = admission_policy("reports.integrated", channel="OFFICIAL_MCP")
    assert (p.app_calls_per_window, p.endpoint_calls_per_window) == (10, 3)


def test_file_upload_declared_and_actual_concurrency_fit_memory_budget(_configured):
    budget = int(_configured["environment"]["MATERIAL_SDK_UPLOAD_MAX_INFLIGHT"])
    declared = admission_policy("materials.upload_video_file")
    actual = admission_policy(
        "materials.upload_video_file",
        channel="OFFICIAL_API",
        physical_endpoint=SDK_ENDPOINTS["materials.upload_video_file"],
    )
    assert declared.endpoint_max_inflight == actual.endpoint_max_inflight == budget == 4


def test_worker_pool_and_prefetch_fit_admission_window(_configured):
    assert _configured["background_pool"] == {"capacity": 32, "prefetch_slots": 12}
    assert _configured["workers"]["worker-ads-backfill"]["concurrency"] == 32
    assert _configured["workers"]["worker-ads-reporting"]["concurrency"] == 24
    assert _configured["polling"]["worker_prefetch_multiplier"] == 1
    # 3条读取队列、管理50线程及其他角色的实际请求数都受应用资源窗口约束。
    assert (
        32 + 24 + 24 + 50 + 6 + 8 + 1
        < _configured["policies"]["base"]["app_max_inflight"]
    )


def test_existing_hot_frequency_keeps_two_facts_paired(_configured):
    from datetime import timedelta

    from app.modules.reporting.planner import (
        HOT_INTERVAL,
        MATERIAL_INTERVAL,
        REFRESH_MARGIN,
    )

    assert (
        HOT_INTERVAL
        == MATERIAL_INTERVAL
        == timedelta(seconds=_configured["polling"]["hot_refresh_seconds"])
    )
    assert REFRESH_MARGIN == HOT_INTERVAL / 2


def test_deployment_transform_preserves_queues_pool_and_exit_grace(_configured):
    import copy
    import importlib.util

    path = Path(__file__).resolve().parents[4] / "deploy/k8s/tiktok_profile.py"
    spec = importlib.util.spec_from_file_location("tiktok_profile", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original = {
        "metadata": {
            "labels": {"app": "tiktok-ad-automation", "role": "worker-ads-backfill"}
        },
        "spec": {
            "replicas": 0,
            "template": {
                "spec": {
                    "terminationGracePeriodSeconds": 750,
                    "containers": [
                        {
                            "command": [
                                "celery",
                                "worker",
                                "-Q",
                                "ads-backfill,ads-directory",
                                "--pool",
                                "prefork",
                                "--concurrency",
                                "12",
                                "--max-memory-per-child",
                                "512000",
                            ],
                            "resources": {},
                        }
                    ],
                }
            },
        },
    }
    before = copy.deepcopy(original)
    result = module.deployment_spec(original, _configured)
    assert original == before
    pod = result["template"]["spec"]
    assert pod["terminationGracePeriodSeconds"] == 750
    assert pod["containers"][0]["command"] == [
        "celery",
        "worker",
        "-Q",
        "ads-backfill,ads-directory",
        "--pool",
        "prefork",
        "--concurrency",
        "32",
        "--max-memory-per-child",
        "512000",
    ]
    assert pod["containers"][0]["resources"]["requests"]["memory"] == "16Gi"
    terms = pod["affinity"]["podAntiAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"
    ]
    assert terms[0]["topologyKey"] == "kubernetes.io/hostname"
    assert terms[0]["labelSelector"]["matchExpressions"][0]["values"] == [
        "worker-ads-backfill",
        "worker-ads-reporting",
    ]
    original["spec"]["template"]["spec"]["containers"][0]["command"] = [
        "celery",
        "worker",
    ]
    with pytest.raises(ValueError, match="explicit worker concurrency"):
        module.deployment_spec(original, _configured)


def test_production_manifest_matches_profile_for_future_full_releases(_configured):
    import yaml

    path = Path(__file__).resolve().parents[4] / "deploy/k8s/tk-ada-workloads.yaml"
    deployments = {
        d["metadata"]["labels"]["role"]: d
        for d in yaml.safe_load_all(path.read_text())
        if d and d["kind"] == "Deployment"
    }
    for role, selected in _configured["workers"].items():
        pod = deployments[role]["spec"]["template"]["spec"]
        container = pod["containers"][0]
        command = container["command"]
        assert command[command.index("--concurrency") + 1] == str(
            selected["concurrency"]
        )
        assert container["resources"] == selected["resources"]
        if selected.get("avoid_colocation_with"):
            terms = pod["affinity"]["podAntiAffinity"][
                "requiredDuringSchedulingIgnoredDuringExecution"
            ]
            assert (
                terms[0]["labelSelector"]["matchExpressions"][0]["values"]
                == selected["avoid_colocation_with"]
            )
