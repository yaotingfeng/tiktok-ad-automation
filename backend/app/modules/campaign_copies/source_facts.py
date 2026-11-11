"""来源只读补充：从实际账户核实CTA和素材，再接入既有素材分发。"""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4, uuid5

from redis import Redis
from sqlalchemy import func
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.builds import CtaCreate
from app.integrations.tiktok.contracts.common import RemoteCallError
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.integrations.tiktok.contracts.materials import (
    ImageRecord,
    RemoteCallBudget,
    VideoRecord,
)
from app.integrations.tiktok.gateway import open_tiktok_gateway
from app.integrations.tiktok.sdk import SDK_SCOPE_INTERRUPTS, AccountAdmissionDeferred
from app.jobs.admission import admission_policy
from app.jobs.celery_app import celery_app
from app.jobs.models import PendingDispatch
from app.jobs.outbox import enqueue_after_commit
from app.jobs.tasks import register_dispatch_task
from app.modules.accounts.routing import verify_route
from app.modules.builds.copy_source import digest, load_copy_source
from app.modules.materials.cover_models import MaterialCoverJob
from app.modules.materials.models import AccountMaterial, MaterialFile
from app.modules.tenants.permissions import require_tenant

from .models import CopySourceFacts

TASK = "campaign_copies.read_source"
HARD_LIMIT = 45
register_dispatch_task(TASK, "resources")


def _queue(session: Session, row: CopySourceFacts, delay: int = 0) -> None:
    row.generation += 1
    row.repair_after = datetime.now(UTC) + timedelta(seconds=delay + 90)
    row.dispatch_id = enqueue_after_commit(
        session,
        context=TenantContext(
            tenant_id=row.tenant_id, actor_id=row.actor_id, role="operator"
        ),
        task_name=TASK,
        task_key=f"copy-source:{row.id}:{row.generation}",
        payload={"facts_id": str(row.id), "generation": row.generation},
    )
    dispatch = session.get(PendingDispatch, row.dispatch_id)
    assert dispatch is not None
    dispatch.available_at = datetime.now(UTC) + timedelta(seconds=delay)
    session.add_all([row, dispatch])


def source_key(source: dict[str, Any]) -> str:
    return digest(
        {key: source[key] for key in ("advertiser_id", "campaign_id")}
        | {
            "materials": sorted(
                (m["video_id"], m["image_id"]) for m in source["materials"]
            ),
            "route": source["snapshot"]["source_route"],
            "cta": source["snapshot"]["cta_portfolio_id"],
        }
    )


def ensure_source_facts(
    session: Session, *, context: TenantContext, source: dict[str, Any]
) -> dict[str, Any]:
    if source["snapshot"]["tenant_id"] != str(context.tenant_id):
        raise DomainError("copy_scope_mismatch", "复制来源不属于当前租户")
    key = source_key(source)
    identity = uuid5(context.tenant_id, "copy-facts:" + key)
    # 同一来源的多个目标账户共享只读准备，不重复创建任何平台资产。
    session.exec(
        select(func.pg_advisory_xact_lock(func.hashtextextended(str(identity), 0)))
    )
    row = session.get(CopySourceFacts, identity, populate_existing=True)
    if row is None:
        row = CopySourceFacts(
            id=identity,
            tenant_id=context.tenant_id,
            bc_id=source["snapshot"]["bc_id"],
            actor_id=context.actor_id,
            source_digest=key,
            source=deepcopy(source),
        )
        session.add(row)
        session.flush()
        _queue(session, row)
    if row.tenant_id != context.tenant_id or row.source_digest != key:
        raise DomainError("copy_source_changed", "来源只读准备身份不一致")
    if row.status == "FAILED":
        raise DomainError(
            row.error_code or "copy_source_read_failed",
            "来源素材或CTA详情读取失败，请刷新来源后重试",
        )
    if row.status != "READY":
        raise DomainError("copy_preparing", "正在读取来源素材与CTA信息", retryable=True)
    return deepcopy(row.facts)


def retry_failed_source(
    session: Session, *, context: TenantContext, source: dict[str, Any]
) -> None:
    """只在用户明确重新预览/读取时开启新一轮只读准备，后台不自行清空失败预算。"""
    key = source_key(source)
    row = session.exec(
        select(CopySourceFacts)
        .where(
            CopySourceFacts.tenant_id == context.tenant_id,
            CopySourceFacts.source_digest == key,
        )
        .with_for_update()
    ).one_or_none()
    if row is not None and row.status == "FAILED":
        _authorize(session, context, source)
        row.source, row.actor_id = deepcopy(source), context.actor_id
        row.status, row.error_code, row.failures = "PENDING", None, 0
        row.claim_token, row.claimed_until = None, None
        row.facts = {}
        _queue(session, row)


def _authorize(
    session: Session, context: TenantContext, source: dict[str, Any]
) -> FrozenTikTokRoute:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="build"
    )
    route = FrozenTikTokRoute.model_validate(source["snapshot"]["source_route"])
    verify_route(
        session,
        context=context,
        route=route,
        advertiser_id=source["advertiser_id"],
        capability="read",
    )
    current = load_copy_source(
        session,
        context=context,
        bc_id=route.bc_id,
        advertiser_id=source["advertiser_id"],
        campaign_id=source["campaign_id"],
    )
    if current["blockers"] or source_key(current) != source_key(source):
        raise DomainError("copy_source_changed", "来源广告配置已变化，请重新载入来源")
    return route


def _record_video(
    session: Session,
    *,
    context: TenantContext,
    route: FrozenTikTokRoute,
    item: dict[str, Any],
    record: VideoRecord | None,
    image: ImageRecord | None,
) -> None:
    """仅登记平台实际返回的尺寸/大小/摘要；没有本地原件就明确unavailable。"""
    if (
        record is None
        or record.video_id != item["video_id"]
        or record.advertiser_id != item["advertiser_id"]
        or record.displayable is not True
        or not record.md5
        or len(record.md5) != 32
        or not record.size
        or record.size <= 0
    ):
        raise DomainError("copy_material_unverified", "平台未返回完整的来源视频信息")
    if (
        image is None
        or image.image_id != item["image_id"]
        or image.advertiser_id != record.advertiser_id
        or image.displayable is not True
        or not image.signature
        or not image.width
        or not image.height
    ):
        raise DomainError("copy_cover_unverified", "平台未返回完整的来源封面信息")
    mid = UUID(item["material_id"])
    session.exec(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended(f"copy-register:{context.tenant_id}:{mid}", 0)
            )
        )
    )
    material = session.get(MaterialFile, mid)
    if material is None:
        material = MaterialFile(
            id=mid,
            tenant_id=context.tenant_id,
            bc_id=route.bc_id,
            file_name=item["file_name"],
            object_key=f"platform-reference/{context.tenant_id}/{mid}",
            byte_size=record.size,
            video_md5=record.md5,
            width=record.width,
            height=record.height,
            duration=record.duration,
            storage_state="unavailable",
            digest_source="tiktok_source_read",
            digest_verified_at=datetime.now(UTC),
        )
        session.add(material)
        session.flush()
    elif (
        material.tenant_id != context.tenant_id
        or material.byte_size != record.size
        or (material.video_md5 and material.video_md5 != record.md5)
    ):
        raise DomainError("copy_material_changed", "来源视频与本地已核实内容不一致")
    elif not material.video_md5:
        material.video_md5 = record.md5
        material.digest_verified_at = datetime.now(UTC)
        material.digest_source = "tiktok_source_read"
        session.add(material)
    asset = session.exec(
        select(AccountMaterial).where(
            AccountMaterial.tenant_id == context.tenant_id,
            AccountMaterial.bc_id == route.bc_id,
            AccountMaterial.material_id == mid,
            AccountMaterial.advertiser_id == record.advertiser_id,
        )
    ).one_or_none()
    if asset is None:
        asset = AccountMaterial(
            tenant_id=context.tenant_id,
            bc_id=route.bc_id,
            material_id=mid,
            advertiser_id=record.advertiser_id,
            connection_id=route.connection_id,
        )
    # 与封面执行器保持先锁封面、后更新账户素材的顺序，避免交叉死锁。
    cover = session.exec(
        select(MaterialCoverJob)
        .where(
            MaterialCoverJob.tenant_id == context.tenant_id,
            MaterialCoverJob.asset_id == asset.id,
            MaterialCoverJob.connection_id == route.connection_id,
            MaterialCoverJob.video_id == record.video_id,
            col(MaterialCoverJob.superseded_by_id).is_(None),
        )
        .with_for_update()
    ).one_or_none()
    asset.connection_id, asset.frozen_route = (
        route.connection_id,
        route.model_dump(mode="json"),
    )
    asset.video_id, asset.mid, asset.image_id = (
        record.video_id,
        record.mid,
        image.image_id,
    )
    asset.status, asset.verified_at = "available", datetime.now(UTC)
    session.add(asset)
    session.flush()
    if cover is None:
        cover = MaterialCoverJob(
            tenant_id=context.tenant_id,
            bc_id=route.bc_id,
            material_id=mid,
            asset_id=asset.id,
            advertiser_id=record.advertiser_id,
            connection_id=route.connection_id,
            actor_id=context.actor_id,
            frozen_route=route.model_dump(mode="json"),
            video_id=record.video_id,
            video_md5=record.md5,
            remote_name="copy-cover-" + mid.hex,
            purpose="SOURCE",
        )
    if cover.claim_token or cover.request_armed_at or cover.share_batch_id:
        # 不改写任何在途封面请求；已有封面不匹配时留给原恢复链核查。
        if (
            cover.status != "READY"
            or (cover.candidate_image_id or cover.known_image_id) != image.image_id
        ):
            raise DomainError(
                "copy_cover_preparing",
                "来源封面已有在途核验，请稍后重试",
                retryable=True,
            )
    else:
        cover.status, cover.candidate_image_id = "READY", image.image_id
        cover.signature, cover.width, cover.height, cover.image_mid = (
            image.signature,
            image.width,
            image.height,
            image.mid,
        )
        session.add(cover)


def process_source(
    *,
    database_engine: Any,
    redis_client: Any,
    tenant_id: UUID,
    actor_id: UUID,
    payload: dict[str, Any],
) -> None:
    if (
        set(payload) != {"facts_id", "generation"}
        or type(payload["generation"]) is not int
        or payload["generation"] < 0
    ):
        raise DomainError("dispatch_payload_invalid", "来源准备任务参数无效")
    try:
        identity = UUID(payload["facts_id"])
    except ValueError, TypeError, AttributeError:
        raise DomainError("dispatch_payload_invalid", "来源准备任务标识无效") from None
    context = TenantContext(tenant_id=tenant_id, actor_id=actor_id, role="operator")
    token = uuid4()
    with Session(database_engine) as session, session.begin():
        row = session.exec(
            select(CopySourceFacts)
            .where(
                CopySourceFacts.id == identity, CopySourceFacts.tenant_id == tenant_id
            )
            .with_for_update()
        ).one_or_none()
        if (
            row is None
            or row.actor_id != actor_id
            or row.generation != payload["generation"]
            or row.status != "PENDING"
        ):
            return
        if row.claimed_until and row.claimed_until > datetime.now(UTC):
            return
        try:
            route = _authorize(session, context, row.source)
        except DomainError as error:
            row.status, row.error_code = "FAILED", error.code
            session.add(row)
            return
        source, facts = deepcopy(row.source), deepcopy(row.facts)
        row.claim_token, row.claimed_until = (
            token,
            datetime.now(UTC) + timedelta(seconds=HARD_LIMIT + 10),
        )
        session.add(row)
    item = next(
        (
            m
            for m in source["materials"]
            if m["material_id"] not in facts.get("materials", [])
        ),
        None,
    )
    deadline = datetime.now(UTC) + timedelta(seconds=HARD_LIMIT - 5)
    result: tuple[VideoRecord | None, ImageRecord | None] | CtaCreate | None = None
    error: Exception | None = None
    try:
        with open_tiktok_gateway(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            route=route,
            task_deadline=deadline,
        ) as gateway:
            if item is None:
                result = gateway.builds.read_cta_portfolio(
                    advertiser_id=source["advertiser_id"],
                    portfolio_id=source["snapshot"]["cta_portfolio_id"],
                )
            else:
                budget = RemoteCallBudget(
                    deadline=deadline,
                    hard_limit_seconds=HARD_LIMIT,
                    lease_ms=admission_policy("materials.get_videos").lease_ms,
                )
                video = gateway.materials.read_video(
                    advertiser_id=source["advertiser_id"],
                    video_id=item["video_id"],
                    budget=budget,
                )
                images = gateway.materials.read_images(
                    advertiser_id=source["advertiser_id"],
                    image_ids=(item["image_id"],),
                    budget=budget,
                )
                result = (video, images[0] if len(images) == 1 else None)
    except SDK_SCOPE_INTERRUPTS:
        raise
    except Exception as caught:
        error = caught
    with Session(database_engine) as session, session.begin():
        row = session.exec(
            select(CopySourceFacts)
            .where(
                CopySourceFacts.id == identity, CopySourceFacts.tenant_id == tenant_id
            )
            .with_for_update()
        ).one()
        if (
            row.claim_token != token
            or row.generation != payload["generation"]
            or row.status != "PENDING"
        ):
            return
        row.claim_token, row.claimed_until = None, None
        try:
            _authorize(session, context, source)
            if error:
                raise error
            if item is None:
                if not isinstance(result, CtaCreate):
                    raise DomainError("copy_cta_unverified", "来源CTA读取结果不完整")
                facts["cta_contents"] = [
                    v.model_dump(mode="json") for v in result.assets
                ]
                row.status = "READY"
            else:
                if not isinstance(result, tuple):
                    raise DomainError(
                        "copy_material_unverified", "来源素材读取结果不完整"
                    )
                with session.begin_nested():
                    _record_video(
                        session,
                        context=context,
                        route=route,
                        item={**item, "advertiser_id": source["advertiser_id"]},
                        record=result[0],
                        image=result[1],
                    )
                facts["materials"] = [*facts.get("materials", []), item["material_id"]]
            row.facts = facts
            if row.status == "PENDING":
                _queue(session, row)
        except SDK_SCOPE_INTERRUPTS:
            raise
        except Exception as caught:
            # 等待调用额度不是读取失败，不消耗失败预算。
            if not isinstance(caught, AccountAdmissionDeferred):
                row.failures += 1
            row.error_code = (
                caught.code
                if isinstance(caught, DomainError)
                else "copy_source_read_failed"
            )
            transient = isinstance(
                caught, (AccountAdmissionDeferred, RemoteCallError)
            ) or getattr(caught, "retryable", False)
            if transient and row.failures < 10:
                _queue(
                    session,
                    row,
                    delay=max(3, getattr(caught, "retry_after_ms", 3000) // 1000),
                )
            else:
                row.status = "FAILED"
        session.add(row)


def repair_sources(session: Session, limit: int = 100) -> int:
    rows = session.exec(
        select(CopySourceFacts)
        .where(
            CopySourceFacts.status == "PENDING",
            CopySourceFacts.repair_after <= datetime.now(UTC),
        )
        .order_by(col(CopySourceFacts.repair_after))
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    for row in rows:
        if not row.claimed_until or row.claimed_until <= datetime.now(UTC):
            row.claim_token, row.claimed_until = None, None
            _queue(session, row)
    return len(rows)


@celery_app.task(
    name=TASK,
    time_limit=HARD_LIMIT,
    soft_time_limit=HARD_LIMIT - 5,
    acks_late=True,
    reject_on_worker_lost=True,
)
def source_task(*, tenant_id: str, actor_id: str, payload: dict[str, Any]) -> None:
    from app.core.db import engine

    client = Redis.from_url(str(settings.REDIS_URL))
    try:
        process_source(
            database_engine=engine,
            redis_client=client,
            tenant_id=UUID(tenant_id),
            actor_id=UUID(actor_id),
            payload=payload,
        )
    finally:
        client.close()
