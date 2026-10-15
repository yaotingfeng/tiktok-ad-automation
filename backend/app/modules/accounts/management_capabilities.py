"""广告管理能力核验与证据保存。

能力是连接授权、BC 绑定、账户角色三者的交集；不以 build/upload 或工具存在
推导管理写权。每一代路由都会生成独立证据，换授权或绑定后旧证据自然失效。
"""

from sqlmodel import Session, select

from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.integrations.tiktok.contracts.management import (
    MANAGEMENT_OPERATIONS,
    ManagementPermissionEvidence,
)
from app.modules.accounts.management_capability_models import ManagementCapability

# 平台接口的对象层级是独立合同；禁止把广告内素材能力扩散为整条广告状态能力。
_ENTITY_KINDS_BY_OPERATION: dict[str, tuple[str, ...]] = {
    "update_roas": ("adgroup",),
    "update_budget": ("campaign", "adgroup"),
    "set_status": ("campaign", "adgroup", "ad"),
    "set_material_status": ("ad",),
}
_MANAGEMENT_ROLES = frozenset({"ADMIN", "OPERATOR"})


def _evidence_payload(evidence: ManagementPermissionEvidence) -> dict:
    return {
        "scope": list(evidence.scope),
        "role": evidence.role,
        "operations": sorted(evidence.operations),
        "source": evidence.source,
        "observed_at": evidence.observed_at.isoformat(),
        "tool_names": list(evidence.tool_names),
    }


def record_management_capabilities(
    session: Session,
    *,
    route: FrozenTikTokRoute,
    advertiser_id: str,
    evidence: ManagementPermissionEvidence,
) -> tuple[ManagementCapability, ...]:
    """按一条 route＋账户记录完整能力矩阵，未证明的项显式为 UNKNOWN。"""

    allowed = evidence.role in _MANAGEMENT_ROLES
    payload = _evidence_payload(evidence)
    rows: list[ManagementCapability] = []
    now = evidence.observed_at
    for operation in MANAGEMENT_OPERATIONS:
        state = (
            "VERIFIED" if allowed and operation in evidence.operations else "UNKNOWN"
        )
        for entity_kind in _ENTITY_KINDS_BY_OPERATION[operation]:
            statement = select(ManagementCapability).where(
                ManagementCapability.tenant_id == route.tenant_id,
                ManagementCapability.bc_id == route.bc_id,
                ManagementCapability.advertiser_id == advertiser_id,
                ManagementCapability.connection_id == route.connection_id,
                ManagementCapability.authorization_revision
                == route.authorization_revision,
                ManagementCapability.binding_revision == route.binding_revision,
                ManagementCapability.adapter_contract_revision
                == route.adapter_contract_revision,
                ManagementCapability.operation == operation,
                ManagementCapability.entity_kind == entity_kind,
            )
            row = session.exec(statement).first()
            if row is None:
                row = ManagementCapability(
                    tenant_id=route.tenant_id,
                    bc_id=route.bc_id,
                    advertiser_id=advertiser_id,
                    connection_id=route.connection_id,
                    authorization_revision=route.authorization_revision,
                    binding_revision=route.binding_revision,
                    adapter_contract_revision=route.adapter_contract_revision,
                    operation=operation,
                    entity_kind=entity_kind,
                )
            row.state = state
            row.verified_at = now if state == "VERIFIED" else None
            row.evidence = payload
            session.add(row)
            rows.append(row)
    session.flush()
    return tuple(rows)
