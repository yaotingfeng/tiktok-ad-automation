"""每项独立校验，校验后衔接已存在的 URL 上传账本和恢复流程。"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.credentials import decrypt_credentials
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.jobs.outbox import enqueue_after_commit
from app.modules.accounts.routing import verify_route
from app.modules.tenants.models import Tenant

from .ingest_models import (
    IngestSession,
    IngestSessionFile,
    TemporaryMaterialObject,
    record_milestone,
)
from .models import MaterialFile
from .push_auth import get_client, require_push_tenant
from .push_models import (
    ExternalMaterialSource,
    MaterialPushBatch,
    MaterialPushItem,
    PushedMaterial,
)
from .push_transport import ExternalFacts, inspect_external, validate_url
from .source_selection import resolve_primary_account

IMPORT_HARD_LIMIT = 660


def external_source_url(
    session: Session, *, context: TenantContext, material_id: UUID
) -> str:
    source = session.get(ExternalMaterialSource, material_id)
    if not source or source.tenant_id != context.tenant_id:
        raise DomainError("push_source_missing", "外部素材来源不存在")
    item = session.get(MaterialPushItem, source.item_id)
    assert item
    batch = session.get(MaterialPushBatch, item.batch_id)
    assert batch
    client = get_client(batch.key_id)
    if client.actor_id != batch.actor_id:
        raise DomainError("push_forbidden", "接入执行用户已改变")
    require_push_tenant(session, client, context.tenant_id)
    url = decrypt_credentials(
        tenant_id=context.tenant_id, ciphertext=item.url_ciphertext
    )["url"]
    validate_url(url)
    return url


def _reuse(
    session: Session,
    batch: MaterialPushBatch,
    item: MaterialPushItem,
    facts: ExternalFacts,
) -> UUID | None:
    # 相同业务身份、内容和名称复用有效副本，未完成的上传也不能因重推而重发。
    candidate = session.exec(
        select(MaterialFile)
        .join(MaterialPushItem, col(MaterialPushItem.material_id) == MaterialFile.id)
        .where(
            MaterialPushItem.tenant_id == batch.tenant_id,
            MaterialPushItem.external_id == item.external_id,
            MaterialFile.tenant_id == batch.tenant_id,
            MaterialFile.bc_id == batch.bc_id,
            MaterialFile.file_name == item.file_name,
            MaterialFile.sha256 == facts.sha256,
            MaterialFile.video_md5 == facts.md5,
            MaterialFile.byte_size == facts.size,
            col(MaterialFile.digest_verified_at).is_not(None),
        )
        .order_by(col(MaterialPushItem.revision).desc())
        .limit(1)
    ).first()
    if candidate is None:
        return None
    pending = session.exec(
        select(IngestSessionFile).where(
            IngestSessionFile.tenant_id == batch.tenant_id,
            IngestSessionFile.material_id == candidate.id,
        )
    ).first()
    if pending and pending.status not in {"available", "failed", "cancelled"}:
        parent = session.get(IngestSession, pending.session_id)
        if not parent or parent.frozen_route != batch.frozen_route:
            raise DomainError(
                "push_prior_upload_pending", "已有上传尚未完成，请先核实原连接任务"
            )
        return candidate.id
    from .remote_sources import resolve_remote_source

    context = TenantContext(batch.tenant_id, batch.actor_id, "operator")
    asset = resolve_remote_source(
        session, context=context, bc_id=batch.bc_id, material_id=candidate.id
    )
    return candidate.id if asset else None


def _publish(
    session: Session,
    batch: MaterialPushBatch,
    item: MaterialPushItem,
    facts: ExternalFacts,
    context: TenantContext,
) -> None:
    route = FrozenTikTokRoute.model_validate(batch.frozen_route)
    verify_route(
        session, context=context, route=route, advertiser_id=None, capability="read"
    )
    resolve_primary_account(
        session, context=context, bc_id=batch.bc_id, route=route, persist=True
    )
    reused = _reuse(session, batch, item, facts)
    if reused:
        source = session.get(ExternalMaterialSource, reused)
        previous = session.get(MaterialPushItem, source.item_id) if source else None
        # 新链接已读回确认同一内容；只让较新修订续期来源，晚到任务不能倒退链接。
        if source and previous and item.revision > previous.revision:
            source.item_id, source.etag = item.id, facts.etag
        item.material_id, item.status = reused, "imported"
        return
    material_id, now = uuid4(), datetime.now(UTC)
    key = f"external/{batch.tenant_id}/{material_id}"
    material = MaterialFile(
        id=material_id,
        tenant_id=batch.tenant_id,
        bc_id=batch.bc_id,
        file_name=item.file_name,
        object_key=key,
        byte_size=facts.size,
        mime_type=facts.mime_type,
        sha256=facts.sha256,
        video_md5=facts.md5,
        duration=facts.duration,
        width=facts.width,
        height=facts.height,
        storage_state="stored",
        current_object_generation=1,
        digest_verified_at=now,
        digest_source="external_stream",
    )
    # 单项完整校验后才知道字节数；建立单文件内部会话，外部批次独立汇总。
    parent = IngestSession(
        tenant_id=batch.tenant_id,
        bc_id=batch.bc_id,
        actor_id=batch.actor_id,
        request_id=item.id,
        request_digest=facts.sha256,
        frozen_route=batch.frozen_route,
        expected_files=1,
        expected_bytes=facts.size,
        status="sealed",
        registration_cursor=1,
    )
    session.add_all([material, parent])
    session.flush()
    row = IngestSessionFile(
        tenant_id=batch.tenant_id,
        bc_id=batch.bc_id,
        session_id=parent.id,
        client_index=0,
        material_id=material.id,
        byte_size=facts.size,
        manifest_digest=facts.sha256,
        status="stored",
    )
    obj = TemporaryMaterialObject(
        tenant_id=batch.tenant_id,
        bc_id=batch.bc_id,
        material_id=material.id,
        generation=1,
        object_key=key,
        storage_provider="external",
        expected_bytes=facts.size,
        actual_bytes=facts.size,
        reserved_bytes=0,
        status="verified",
        sha256=facts.sha256,
        video_md5=facts.md5,
        digest_verified_at=now,
        digest_source="external_stream",
        received_at=now,
    )
    session.add_all([row, obj])
    session.flush()
    session.add(
        ExternalMaterialSource(
            material_id=material.id,
            tenant_id=batch.tenant_id,
            item_id=item.id,
            etag=facts.etag,
        )
    )
    item.material_id, item.status = material.id, "imported"
    for milestone in ("accepted", "uploaded"):
        record_milestone(
            session,
            tenant_id=batch.tenant_id,
            bc_id=batch.bc_id,
            session_id=parent.id,
            material_id=material.id,
            milestone=milestone,
        )
    row.dispatch_id = enqueue_after_commit(
        session,
        context=context,
        task_name="materials.upload_original",
        task_key=f"source-ingest:{obj.id}:1",
        payload={
            "material_id": str(material.id),
            "object_id": str(obj.id),
            "generation": 1,
        },
    )


def process_item(
    *, database_engine: Any, context: TenantContext, item_id: UUID
) -> None:
    owner, now = uuid4(), datetime.now(UTC)
    with Session(database_engine) as db, db.begin():
        item = db.exec(
            select(MaterialPushItem)
            .where(
                MaterialPushItem.id == item_id,
                MaterialPushItem.tenant_id == context.tenant_id,
            )
            .with_for_update()
        ).one_or_none()
        if (
            not item
            or item.status in {"imported", "failed"}
            or (item.claimed_until and item.claimed_until > now)
        ):
            return
        batch = db.get(MaterialPushBatch, item.batch_id)
        assert batch
        if batch.actor_id != context.actor_id:
            raise DomainError("push_forbidden", "任务操作人不匹配")
        item.claim_token, item.claimed_until = (
            owner,
            now + timedelta(seconds=IMPORT_HARD_LIMIT + 30),
        )
        item.status, item.attempts = "validating", item.attempts + 1
        ciphertext, tenant_id, key_id, file_name = (
            item.url_ciphertext,
            item.tenant_id,
            batch.key_id,
            item.file_name,
        )
    facts, error = None, None
    try:
        client = get_client(key_id)
        with Session(database_engine) as db:
            if client.actor_id != context.actor_id:
                raise DomainError("push_forbidden", "推送执行用户已改变")
            require_push_tenant(db, client, tenant_id)
        if not settings.MATERIAL_INGEST_ENABLED:
            raise DomainError("material_ingest_disabled", "素材入库暂未启用")
        url = decrypt_credentials(tenant_id=tenant_id, ciphertext=ciphertext)["url"]
        facts = inspect_external(url, file_name)
    except Exception as exc:
        error = exc.code if isinstance(exc, DomainError) else "push_validation_failed"
    with Session(database_engine) as db, db.begin():
        # 与接收路径保持 tenant -> item 锁序，同租户相同内容发布串行复用。
        db.exec(select(Tenant).where(Tenant.id == tenant_id).with_for_update()).one()
        item = db.exec(
            select(MaterialPushItem)
            .where(MaterialPushItem.id == item_id)
            .with_for_update()
        ).one()
        if item.claim_token != owner:
            return
        item.claim_token, item.claimed_until = None, None
        batch = db.get(MaterialPushBatch, item.batch_id)
        assert batch
        if facts is not None:
            try:
                with db.begin_nested():
                    client = get_client(key_id)
                    if client.actor_id != context.actor_id:
                        raise DomainError("push_forbidden", "推送执行用户已改变")
                    current = require_push_tenant(db, client, tenant_id)
                    if not settings.MATERIAL_INGEST_ENABLED:
                        raise DomainError(
                            "material_ingest_disabled", "素材入库暂未启用"
                        )
                    _publish(db, batch, item, facts, current)
                    head = db.get(PushedMaterial, (tenant_id, item.external_id))
                    if head and head.latest_revision == item.revision:
                        head.current_material_id = item.material_id
                    db.flush()
            except DomainError as exc:
                error = exc.code
        if error:
            item.status, item.error_code = "failed", error


def repair_imports(session: Session, *, limit: int = 100) -> int:
    from app.jobs.models import PendingDispatch

    now, queued = datetime.now(UTC), 0
    stale_dispatch = (
        select(PendingDispatch.id)
        .where(
            PendingDispatch.id == MaterialPushItem.dispatch_id,
            col(PendingDispatch.published_at)
            <= now - timedelta(seconds=IMPORT_HARD_LIMIT + 30),
        )
        .exists()
    )
    items = session.exec(
        select(MaterialPushItem)
        .where(
            col(MaterialPushItem.status).in_(("queued", "validating")),
            (col(MaterialPushItem.claimed_until).is_(None))
            | (col(MaterialPushItem.claimed_until) <= now),
            # 在 LIMIT 前排除正常排队项，防止前 100 项让过期领取永远饥饿。
            (col(MaterialPushItem.status) == "validating")
            | col(MaterialPushItem.dispatch_id).is_(None)
            | stale_dispatch,
        )
        .order_by(col(MaterialPushItem.id))
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    for item in items:
        pending = (
            session.get(PendingDispatch, item.dispatch_id) if item.dispatch_id else None
        )
        if item.attempts >= 3:
            item.status, item.error_code = "failed", "push_worker_interrupted"
            item.claim_token, item.claimed_until = None, None
            continue
        batch = session.get(MaterialPushBatch, item.batch_id)
        assert batch
        item.claim_token, item.claimed_until, item.status = None, None, "queued"
        item.dispatch_id = enqueue_after_commit(
            session,
            context=TenantContext(batch.tenant_id, batch.actor_id, "operator"),
            task_name="materials.import_external",
            task_key=f"external:{item.id}:{item.attempts}",
            payload={"item_id": str(item.id)},
        )
        if item.dispatch_id and pending and item.dispatch_id == pending.id:
            pending.published_at, pending.available_at = None, now
        queued += 1
    return queued
