"""素材各自保存发送归属；恢复只能核验，不能重选今日默认。"""

from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import CheckConstraint
from sqlmodel import Session

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.modules.accounts.routing import Capability, verify_route


def route_constraint(
    table: str, column: str, *, connection: bool = False
) -> CheckConstraint:
    keys = "ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision']"
    conditions = [
        f"jsonb_typeof({column}) = 'object'",
        f"{column} ?& {keys}",
        f"{column} - {keys} = '{{}}'::jsonb",
        *[
            f"jsonb_typeof({column}->'{key}') = '{kind}'"
            for key, kind in (
                ("tenant_id", "string"),
                ("bc_id", "string"),
                ("connection_id", "string"),
                ("channel", "string"),
                ("authorization_revision", "number"),
                ("adapter_contract_revision", "string"),
            )
        ],
        f"{column}->>'tenant_id' = tenant_id::text",
        f"{column}->>'bc_id' = bc_id",
        f"length(btrim({column}->>'bc_id')) > 0",
        f"{column}->>'connection_id' ~ '^[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}$'",
        f"{column}->>'channel' IN ('OFFICIAL_API','OFFICIAL_MCP')",
        f"{column}->>'authorization_revision' ~ '^[0-9]+$'",
        f"length(btrim({column}->>'adapter_contract_revision')) > 0",
    ]
    if connection:
        conditions.append(f"{column}->>'connection_id' = connection_id::text")
    return CheckConstraint(
        f"{column} IS NULL OR COALESCE((" + " AND ".join(conditions) + "), false)",
        name=f"ck_{table}_{column}",
    )


def load_material_route(
    value: dict[str, Any] | None,
    *,
    context: TenantContext,
    bc_id: str,
    connection_id: UUID | None = None,
) -> FrozenTikTokRoute:
    # 历史 JSON 缺版本不是重新选路的许可；NULL 和畸形历史均需显式核实。
    try:
        if value is None:
            raise ValueError("missing route")
        route = FrozenTikTokRoute.model_validate(value)
        if (
            type(value.get("authorization_revision")) is not int
            or route.authorization_revision < 0
        ):
            raise ValueError("invalid revision")
        if not route.bc_id.strip() or not route.adapter_contract_revision.strip():
            raise ValueError("missing scope")
    except (ValueError, ValidationError) as error:
        raise DomainError(
            "material_route_unverified", "历史素材连接信息需要核实"
        ) from error
    if (route.tenant_id, route.bc_id) != (context.tenant_id, bc_id) or (
        connection_id is not None and route.connection_id != connection_id
    ):
        raise DomainError("frozen_route_scope_mismatch", "素材与冻结连接范围不匹配")
    return route


def require_material_route(
    session: Session,
    *,
    context: TenantContext,
    route: FrozenTikTokRoute,
    bc_id: str,
    advertiser_id: str,
    capability: Capability,
) -> None:
    load_material_route(route.model_dump(mode="json"), context=context, bc_id=bc_id)
    verify_route(
        session,
        context=context,
        route=route,
        advertiser_id=advertiser_id,
        capability=capability,
    )


def require_same_route(actual: FrozenTikTokRoute, expected: FrozenTikTokRoute) -> None:
    if actual != expected:
        raise DomainError("frozen_route_changed", "已有素材任务的执行连接不能改变")


def require_sdk_route(route: FrozenTikTokRoute) -> None:
    # P2.3/4 完成前旧写入口仅有 API 证据，绝不能用 MCP 凭据调用 API。
    if route.channel != "OFFICIAL_API":
        raise DomainError("material_channel_unverified", "当前素材通道能力尚未核实")


def source_parent_route(
    session: Session,
    *,
    context: TenantContext,
    material_id: UUID,
    bc_id: str,
    generation: int | None,
) -> FrozenTikTokRoute:
    from sqlmodel import col, select

    from .ingest_models import IngestSession, IngestSessionFile
    from .models import ObjectUpload, UploadBatch

    if generation is not None:
        values = session.exec(
            select(IngestSession.frozen_route)
            .join(
                IngestSessionFile,
                (col(IngestSessionFile.session_id) == IngestSession.id)
                & (col(IngestSessionFile.tenant_id) == IngestSession.tenant_id),
            )
            .where(
                IngestSession.tenant_id == context.tenant_id,
                IngestSession.bc_id == bc_id,
                IngestSessionFile.material_id == material_id,
            )
            .limit(2)
        ).all()
    else:
        values = session.exec(
            select(UploadBatch.frozen_route)
            .join(
                ObjectUpload,
                (col(ObjectUpload.batch_id) == UploadBatch.id)
                & (col(ObjectUpload.tenant_id) == UploadBatch.tenant_id),
            )
            .where(
                UploadBatch.tenant_id == context.tenant_id,
                UploadBatch.bc_id == bc_id,
                ObjectUpload.material_id == material_id,
            )
            .limit(2)
        ).all()
    if len(values) != 1:
        raise DomainError("material_route_unverified", "历史素材连接信息需要核实")
    return load_material_route(
        dict(values[0]) if values[0] is not None else None, context=context, bc_id=bc_id
    )
