"""纠正流程专用的固定补充合同；不改变历史搭建路由的主 manifest。"""

import hashlib
import json
from dataclasses import asdict
from uuid import UUID

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.builds import FrozenModel, Id
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.integrations.tiktok.mcp.protocol import OFFICIAL_ENDPOINT, ToolContract


class FrozenGroupIsolation(FrozenModel):
    """调用方持久 ledger 的冻结坐标；不是凭此对象即可取得写入授权。"""

    ledger_id: UUID
    route: FrozenTikTokRoute
    advertiser_id: Id
    adgroup_id: Id
    contract_revision: Id


def isolation_contracts() -> tuple[ToolContract, ...]:
    # 仅提取 2026-09-15 官方 tools/list 的两个完整语义 schema，不加载私有目录。
    # 上游允许 ENABLE/DELETE，业务方法只编译 DISABLE；不能篡改 schema 伪装已核实。
    return (
        ToolContract(
            operation="build.disable_adgroup",
            tool_name="smart_plus_adgroup_status_update",
            effect="WRITE",
            input_schema={
                "type": "object",
                "properties": {
                    "adgroup_ids": {"items": {"type": "string"}, "type": "array"},
                    "advertiser_id": {"type": "string"},
                    "operation_status": {
                        "enum": ["DISABLE", "ENABLE", "DELETE"],
                        "type": "string",
                    },
                },
                "required": ["advertiser_id", "adgroup_ids", "operation_status"],
            },
            output_schema=None,
            response_shape="OBJECT",
            source_urls=(
                OFFICIAL_ENDPOINT,
                "https://business-api.tiktok.com/open_api/v1.3/smart_plus/adgroup/status/update/",
            ),
            evidence="OBSERVED",
            text_json_envelope=True,
        ),
        ToolContract(
            operation="build.list_optimizer_rules",
            tool_name="optimizer_rule_list_get",
            effect="READ",
            input_schema={
                "type": "object",
                "properties": {
                    "advertiser_id": {"type": "string"},
                    "filtering": {
                        "properties": {
                            "data_dimension": {
                                "enum": ["CAMPAIGN", "ADGROUP", "AD"],
                                "type": "string",
                            },
                            "rule_info": {"items": {"type": "string"}, "type": "array"},
                            "status": {
                                "enum": ["ON", "OFF", "DELETED"],
                                "type": "string",
                            },
                        },
                        "type": "object",
                    },
                    "page": {"format": "double", "type": "number"},
                    "page_size": {"format": "double", "type": "number"},
                    "tzone": {"type": "string"},
                },
                "required": ["advertiser_id"],
            },
            output_schema=None,
            response_shape="OBJECT",
            source_urls=(
                OFFICIAL_ENDPOINT,
                "https://business-api.tiktok.com/open_api/v1.3/optimizer/rule/list/",
            ),
            evidence="OBSERVED",
            text_json_envelope=True,
        ),
    )


def group_isolation_contract_revision() -> str:
    encoded = json.dumps(
        [asdict(contract) for contract in isolation_contracts()],
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def require_isolation_target(
    scope: FrozenGroupIsolation | None,
    *,
    advertiser_id: str,
    adgroup_id: str | None = None,
) -> None:
    if scope is None:
        raise DomainError(
            "group_isolation_authority_required", "该操作需要纠正隔离账本授权"
        )
    if advertiser_id != scope.advertiser_id or (
        adgroup_id is not None and adgroup_id != scope.adgroup_id
    ):
        raise DomainError("group_isolation_scope_mismatch", "操作超出冻结隔离范围")


def optimizer_rule_arguments(*, advertiser_id: str, page: int) -> dict[str, object]:
    # 不过滤 OFF/层级/名称，保留完整目录供调用方核查可能重新启用的规则。
    if type(page) is not int or not 1 <= page <= 1000:
        raise DomainError("group_isolation_page_invalid", "自动规则页码无效")
    return {"advertiser_id": advertiser_id, "page": page, "page_size": 100}
