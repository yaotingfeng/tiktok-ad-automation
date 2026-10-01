"""广告管理能力核验与证据保存。

能力是连接授权、BC 绑定、账户角色三者的交集；不以 build/upload 或工具存在
推导管理写权。每一代路由都会生成独立证据，换授权或绑定后旧证据自然失效。
"""

from datetime import UTC, datetime, timedelta

from redis import Redis
from sqlalchemy import Engine
from sqlmodel import Session, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.integrations.tiktok.contracts.management import (
    MANAGEMENT_OPERATIONS,
    ManagementPermissionEvidence,
)
from app.integrations.tiktok.gateway import open_tiktok_gateway
from app.modules.accounts.connection_models import (
    BCAccountAccess,
)
from app.modules.accounts.management_capability_models import ManagementCapability

# 平台接口的对象层级是独立合同；禁止把广告内素材能力扩散为整条广告状态能力。
_ENTITY_KINDS_BY_OPERATION: dict[str, tuple[str, ...]] = {
    "update_roas": ("campaign", "adgroup"),
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
        state = "VERIFIED" if allowed and operation in evidence.operations else "UNKNOWN"
        for entity_kind in _ENTITY_KINDS_BY_OPERATION[operation]:
            statement = select(ManagementCapability).where(
                ManagementCapability.tenant_id == route.tenant_id,
                ManagementCapability.bc_id == route.bc_id,
                ManagementCapability.advertiser_id == advertiser_id,
                ManagementCapability.connection_id == route.connection_id,
                ManagementCapability.authorization_revision == route.authorization_revision,
                ManagementCapability.binding_revision == route.binding_revision,
                ManagementCapability.adapter_contract_revision == route.adapter_contract_revision,
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


def _role_for_account(session: Session, route: FrozenTikTokRoute, advertiser_id: str) -> str | None:
    # 复用现有完整角色采集结果；不要通过发送广告写入试探权限。
    grant = session.get(BCAccountAccess, (route.tenant_id, route.bc_id, advertiser_id, route.connection_id))
    role = getattr(grant, "role", None)
    if role in {"ADMIN", "OPERATOR", "ANALYST", "STANDARD"}:
        return role
    return None


def refresh_management_capabilities(
    database_engine: Engine,
    *,
    context: TenantContext,
    route: FrozenTikTokRoute,
    advertiser_id: str,
) -> tuple[ManagementCapability, ...]:
    """只读读取当前连接授权事实与账户角色，然后持久化管理能力。"""

    if route.tenant_id != context.tenant_id:
        raise DomainError("connection_tenant_mismatch", "连接不属于当前租户")
    # 该刷新是账户读取，不持有管理写事务；Redis 只供 gateway 的共享准入使用。
    with Redis.from_url(settings.REDIS_URL, decode_responses=True) as redis_client:
        with open_tiktok_gateway(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            route=route,
            task_deadline=datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=45),
        ) as gateway:
            facts = gateway.accounts.authorization_facts()
            # 账户角色读取使用既有分页合同，读取不足一页时继续请求直到完整。
            role: str | None = None
            page = 1
            while True:
                roles = gateway.accounts.roles(bc_id=route.bc_id, page=page, page_size=100)
                for item in roles.items:
                    if item.advertiser_id == advertiser_id:
                        role = item.role
                if roles.last:
                    break
                page += 1
            with Session(database_engine) as session:
                role = role or _role_for_account(session, route, advertiser_id)
                evidence = ManagementPermissionEvidence(
                    scope=facts.scopes,
                    role=role,
                    operations=facts.management_operations,
                    source=facts.evidence_source,
                    observed_at=facts.observed_at,
                )
                rows = record_management_capabilities(
                    session, route=route, advertiser_id=advertiser_id, evidence=evidence
                )
                session.commit()
                return rows
