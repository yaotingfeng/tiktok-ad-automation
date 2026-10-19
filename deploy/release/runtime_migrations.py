"""与已审查的数据合同升级一起应用运行参数；旧配置先进入可恢复备份。"""

import hashlib
import json

from common import require

SYNC_SOURCE_HEAD = "0038_sync_work_indexes"
SYNC_SOURCE_POLICY = "600215f3c7f3c76f8acbf9907e54ca95ca4e2afb05c958d9942afa7a60e58c04"
SYNC_READ_OPERATIONS = (
    "reports.integrated",
    "reports.bc",
    "reports.material_overview",
    "reports.material_breakdown",
    "ads.get_campaigns",
    "build.get_regular_adgroups",
    "ads.get_ads",
    "build.get_campaigns",
    "build.get_adgroups",
    "build.get_ads",
)


def sync_runtime_values(values, source_heads):
    """仅迁移已批准的0038生产配置；0039及后续发布不覆盖运维的新参数。"""
    if source_heads != [SYNC_SOURCE_HEAD]:
        return values
    policy = json.loads(values["TIKTOK_CALL_POLICIES"])
    fingerprint = hashlib.sha256(
        json.dumps(policy, sort_keys=True).encode()
    ).hexdigest()
    require(
        fingerprint == SYNC_SOURCE_POLICY,
        "Source call policy differs from approved sync migration",
    )
    require(
        values.get("TIKTOK_REPORT_DAILY_CALL_BUDGET") == "691200",
        "Source daily budget differs from approved sync migration",
    )
    for operation in SYNC_READ_OPERATIONS:
        policy.setdefault("endpoints", {})["OFFICIAL_API:" + operation] = {
            "endpoint_calls_per_window": 10,
            "endpoint_max_inflight": 6,
        }
    return values | {
        "TIKTOK_CALL_POLICIES": json.dumps(policy, separators=(",", ":")),
        "TIKTOK_REPORT_DAILY_CALL_BUDGET": "0",
    }
