"""批量原子登记；出网校验另由 worker 执行。"""

from datetime import UTC
from hashlib import sha256
from uuid import UUID

from sqlalchemy import func
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.credentials import encrypt_credentials
from app.core.errors import DomainError
from app.jobs.outbox import enqueue_after_commit
from app.jobs.tasks import register_dispatch_task
from app.modules.accounts.connection_models import BCConnectionBinding
from app.modules.accounts.models import TenantBC
from app.modules.accounts.routing import freeze_route
from app.modules.tenants.models import Tenant

from .ingest_models import IngestSessionFile
from .models import AccountMaterial
from .push_auth import PushIdentity, require_push_tenant
from .push_models import MaterialPushBatch, MaterialPushItem, PushedMaterial
from .push_schemas import PushBatchInput, PushBatchPublic, PushItemPublic
from .push_transport import validate_url
from .source_selection import resolve_primary_account

register_dispatch_task("materials.import_external", "resources")


def select_push_bc(session: Session, tenant: Tenant) -> str:
    if tenant.default_bc_id:
        return tenant.default_bc_id
    # 与工作区 BC 目录保持相同顺序和解绑规则，不按上传权限暗中换 BC。
    bindings = select(BCConnectionBinding.connection_id).where(
        BCConnectionBinding.tenant_id == tenant.id,
        BCConnectionBinding.bc_id == TenantBC.bc_id,
    )
    bc = session.exec(
        select(TenantBC.bc_id)
        .where(
            TenantBC.tenant_id == tenant.id,
            (~bindings.exists())
            | bindings.where(BCConnectionBinding.status != "DISABLED").exists(),
        )
        .order_by(col(TenantBC.bc_id))
        .limit(1)
    ).first()
    if not bc:
        raise DomainError("push_bc_missing", "租户尚未配置 BC")
    return bc


def batch_public(session: Session, batch: MaterialPushBatch) -> PushBatchPublic:
    items = session.exec(
        select(MaterialPushItem)
        .where(
            MaterialPushItem.tenant_id == batch.tenant_id,
            MaterialPushItem.batch_id == batch.id,
        )
        .order_by(col(MaterialPushItem.external_id))
    ).all()
    values = []
    for item in items:
        status, error, advertiser, vid = item.status, item.error_code, None, None
        if item.material_id:
            asset = session.exec(
                select(AccountMaterial)
                .where(
                    AccountMaterial.tenant_id == batch.tenant_id,
                    AccountMaterial.bc_id == batch.bc_id,
                    AccountMaterial.material_id == item.material_id,
                    AccountMaterial.status == "available",
                )
                .order_by(col(AccountMaterial.id))
                .limit(1)
            ).first()
            row = session.exec(
                select(IngestSessionFile).where(
                    IngestSessionFile.tenant_id == batch.tenant_id,
                    IngestSessionFile.material_id == item.material_id,
                )
            ).first()
            if asset:
                status, advertiser, vid = (
                    "available",
                    asset.advertiser_id,
                    asset.video_id,
                )
            elif row:
                status, error, advertiser = (
                    row.status,
                    row.error_code,
                    row.source_advertiser_id,
                )
        values.append(
            PushItemPublic(
                material_id=item.external_id,
                revision=item.revision,
                file_name=item.file_name,
                library_material_id=item.material_id,
                status=status,
                error_code=error,
                advertiser_id=advertiser,
                video_id=vid,
            )
        )
    states = {item.status for item in values}
    if states == {"available"}:
        status = "completed"
    elif states <= {"failed", "blocked", "cancelled", "available"}:
        status = "partial_failed" if "available" in states else "failed"
    elif states == {"queued"}:
        status = "accepted"
    else:
        status = "processing"
    return PushBatchPublic(
        batch_id=batch.id,
        request_id=batch.request_id,
        tenant_name=batch.tenant_name,
        bc_id=batch.bc_id,
        status=status,
        accepted_count=len(values),
        created_at=batch.created_at.astimezone(UTC),
        materials=values,
    )


def read_batch(
    session: Session, *, identity: PushIdentity, batch_id: UUID
) -> PushBatchPublic:
    batch = session.get(MaterialPushBatch, batch_id)
    if not batch or batch.key_id != identity.key_id:
        raise DomainError("push_batch_not_found", "批次不存在或不可见")
    require_push_tenant(session, identity.client, batch.tenant_id)
    return batch_public(session, batch)


def register_batch(
    session: Session, *, identity: PushIdentity, body: PushBatchInput, raw_body: bytes
) -> PushBatchPublic:
    request_digest = sha256(raw_body).hexdigest()
    lock = int.from_bytes(
        sha256(f"push:{identity.key_id}:{identity.request_id}".encode()).digest()[:8],
        "big",
        signed=True,
    )
    session.exec(select(func.pg_advisory_xact_lock(lock))).one()
    existing = session.exec(
        select(MaterialPushBatch).where(
            MaterialPushBatch.key_id == identity.key_id,
            MaterialPushBatch.request_id == identity.request_id,
        )
    ).one_or_none()
    if existing:
        require_push_tenant(session, identity.client, existing.tenant_id)
        if existing.request_digest != request_digest:
            raise DomainError("idempotency_conflict", "请求编号已绑定其他内容")
        return batch_public(session, existing)
    if not settings.MATERIAL_INGEST_ENABLED:
        raise DomainError("material_ingest_disabled", "素材入库暂未启用")
    tenants = session.exec(
        select(Tenant).where(Tenant.name == body.tenant_name).with_for_update()
    ).all()
    if len(tenants) != 1 or not tenants[0].active:
        raise DomainError("push_tenant_invalid", "租户不存在、已停用或名称不唯一")
    tenant = tenants[0]
    context = require_push_tenant(session, identity.client, tenant.id)
    for item in body.materials:
        validate_url(item.url)
    bc_id = select_push_bc(session, tenant)
    route = freeze_route(session, context=context, bc_id=bc_id)
    resolve_primary_account(
        session, context=context, bc_id=bc_id, route=route, persist=True
    )
    batch = MaterialPushBatch(
        tenant_id=tenant.id,
        tenant_name=tenant.name,
        bc_id=bc_id,
        actor_id=context.actor_id,
        key_id=identity.key_id,
        request_id=identity.request_id,
        request_digest=request_digest,
        frozen_route=route.model_dump(mode="json"),
    )
    session.add(batch)
    session.flush()
    for value in body.materials:
        head = session.get(PushedMaterial, (tenant.id, value.material_id))
        if head is None:
            head = PushedMaterial(tenant_id=tenant.id, external_id=value.material_id)
            session.add(head)
        else:
            head.latest_revision += 1
        session.flush()
        item = MaterialPushItem(
            tenant_id=tenant.id,
            batch_id=batch.id,
            external_id=value.material_id,
            revision=head.latest_revision,
            file_name=value.file_name,
            url_ciphertext=encrypt_credentials(
                tenant_id=tenant.id, value={"url": value.url}
            ),
        )
        session.add(item)
        session.flush()
        item.dispatch_id = enqueue_after_commit(
            session,
            context=context,
            task_name="materials.import_external",
            task_key=f"external:{item.id}:0",
            payload={"item_id": str(item.id)},
        )
    session.flush()
    return batch_public(session, batch)
