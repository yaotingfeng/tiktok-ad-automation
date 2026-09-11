"""Permanent target-cover upload identities with bounded, read-only recovery."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from math import ceil
from typing import Any
from uuid import UUID, uuid4

from redis import Redis
from sqlalchemy import Engine, func, or_
from sqlalchemy.dialects.postgresql import array, insert
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.bounded_resources import bounded_session
from app.integrations.tiktok.contracts import materials as material_types
from app.integrations.tiktok.contracts.common import RemoteCallError
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.integrations.tiktok.gateway import open_tiktok_gateway
from app.integrations.tiktok.sdk import (
    AccountAdmissionDeferred,
)
from app.jobs.admission import admission_policy
from app.jobs.models import PendingDispatch
from app.jobs.outbox import enqueue_after_commit
from app.jobs.tasks import register_dispatch_task
from app.modules.tenants.permissions import require_tenant

from . import cover_sdk as api
from .cover_models import MaterialCoverJob, MaterialCoverJobPage, MaterialCoverReceipt
from .models import AccountMaterial, MaterialFile
from .repository import asset_public, require_material_scope
from .routes import (
    load_material_route,
    require_material_route,
    require_same_route,
)
from .schemas import AssetPreparation

HARD_LIMIT = 45
SOFT_LIMIT = 40
CLAIM_SECONDS = 60
register_dispatch_task("materials.prepare_cover", "resources")
register_dispatch_task("materials.verify_cover", "resources")


def _now() -> datetime:
    return datetime.now(UTC)


def _mapping(session: Session, job: MaterialCoverJob) -> AccountMaterial | None:
    asset = session.get(AccountMaterial, job.asset_id, populate_existing=True)
    if (
        asset
        and (
            asset.tenant_id,
            asset.bc_id,
            asset.material_id,
            asset.advertiser_id,
            asset.video_id,
            asset.status,
        )
        == (
            job.tenant_id,
            job.bc_id,
            job.material_id,
            job.advertiser_id,
            job.video_id,
            "available",
        )
        and asset.verified_at
    ):
        return asset
    return None


def _access(
    session: Session,
    context: TenantContext,
    job: MaterialCoverJob,
    *,
    upload: bool = False,
) -> None:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="build"
    )
    route = load_material_route(
        job.frozen_route,
        context=context,
        bc_id=job.bc_id,
        connection_id=job.connection_id,
    )
    require_material_route(
        session,
        context=context,
        route=route,
        bc_id=job.bc_id,
        advertiser_id=job.advertiser_id,
        capability="read",
    )
    if upload:
        # 上传所用URL来自本账户刚核实的视频；读权与上传权必须同时仍有效。
        require_material_route(
            session,
            context=context,
            route=route,
            bc_id=job.bc_id,
            advertiser_id=job.advertiser_id,
            capability="upload",
        )
    if code := _digest_error(session, job):
        raise DomainError(code, "原视频摘要缺失或已变化，请核实素材")
    if _mapping(session, job) is None:
        raise DomainError(
            "cover_video_changed", "目标视频或连接已变化，请重新准备当前素材"
        )


def _digest_error(session: Session, job: MaterialCoverJob) -> str | None:
    if job.video_md5 is None:
        return "cover_video_unverified"
    material = session.get(MaterialFile, job.material_id, populate_existing=True)
    if (
        material is None
        or (material.tenant_id, material.bc_id) != (job.tenant_id, job.bc_id)
        or material.video_md5 != job.video_md5
    ):
        return "cover_video_changed"
    return None


def _fresh(job: MaterialCoverJob) -> bool:
    return job.updated_at >= _now() - timedelta(
        seconds=settings.MATERIAL_ASSET_MAX_AGE_SECONDS
    )


def _result(session: Session, job: MaterialCoverJob) -> AssetPreparation:
    if job.error_code == "cover_receipt_ambiguous":
        return AssetPreparation(
            state="blocked", task_id=job.id, reason_code="cover_receipt_ambiguous"
        )
    if code := _digest_error(session, job):
        return AssetPreparation(state="blocked", task_id=job.id, reason_code=code)
    mapping = _mapping(session, job)
    if mapping is None:
        return AssetPreparation(
            state="blocked", task_id=job.id, reason_code="cover_video_changed"
        )
    if job.status == "READY" and not _fresh(job):
        return AssetPreparation(
            state="blocked", task_id=job.id, reason_code="cover_evidence_stale"
        )
    if job.status == "READY" and mapping.image_id == job.known_image_id:
        return AssetPreparation(
            state="ready", mapping=asset_public(mapping), task_id=job.id
        )
    if job.status in {"UNKNOWN", "BLOCKED"} or job.error_code == "cover_result_unknown":
        return AssetPreparation(
            state="blocked",
            task_id=job.id,
            reason_code="cover_result_unknown"
            if job.status == "UNKNOWN"
            else job.error_code,
        )
    return AssetPreparation(state="queued", task_id=job.id, reason_code=job.error_code)


def _queue(
    session: Session, job: MaterialCoverJob, *, read: bool, delay: int = 0
) -> None:
    job.revision += 1
    job.claim_token = job.claimed_until = None
    job.status = "VERIFYING" if read else "PENDING"
    job.updated_at = _now()
    job.repair_after = _now() + timedelta(seconds=delay + CLAIM_SECONDS)
    job.dispatch_id = enqueue_after_commit(
        session,
        context=TenantContext(
            tenant_id=job.tenant_id, actor_id=job.actor_id, role="operator"
        ),
        task_name="materials.verify_cover" if read else "materials.prepare_cover",
        task_key=f"cover:{job.id}:{job.revision}",
        payload={"job_id": str(job.id), "revision": job.revision},
    )
    dispatch = session.get(PendingDispatch, job.dispatch_id)
    assert dispatch
    dispatch.available_at = _now() + timedelta(seconds=delay)


def ensure_cover(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    material_id: UUID,
    advertiser_id: str,
    task_key: str,
    route: FrozenTikTokRoute,
) -> AssetPreparation:
    # Caller task keys cannot create a second upload identity for the same VID.
    if not task_key or len(task_key) > 255:
        raise DomainError("invalid_asset_task", "封面任务标识无效")
    require_material_scope(session, context=context, bc_id=bc_id)
    require_material_route(
        session,
        context=context,
        route=route,
        bc_id=bc_id,
        advertiser_id=advertiser_id,
        capability="build",
    )
    material = session.exec(
        select(MaterialFile)
        .where(
            MaterialFile.tenant_id == context.tenant_id,
            MaterialFile.bc_id == bc_id,
            MaterialFile.id == material_id,
        )
        .with_for_update()
    ).first()
    if material is None:
        raise DomainError("material_not_found", "未找到当前租户 BC 素材")
    asset = session.exec(
        select(AccountMaterial)
        .where(
            AccountMaterial.tenant_id == context.tenant_id,
            AccountMaterial.bc_id == bc_id,
            AccountMaterial.material_id == material_id,
            AccountMaterial.advertiser_id == advertiser_id,
        )
        .with_for_update()
    ).first()
    if (
        not asset
        or asset.status != "available"
        or not asset.video_id.strip()
        or not asset.verified_at
    ):
        return AssetPreparation(state="blocked", reason_code="cover_video_not_ready")
    jobs = session.exec(
        select(MaterialCoverJob)
        .where(
            MaterialCoverJob.tenant_id == context.tenant_id,
            MaterialCoverJob.asset_id == asset.id,
            MaterialCoverJob.video_id == asset.video_id,
        )
        .with_for_update()
        .limit(2)
    ).all()
    if len(jobs) > 1:
        raise DomainError("material_route_unverified", "历史素材连接信息需要核实")
    job = jobs[0] if jobs else None
    if job:
        require_same_route(
            load_material_route(job.frozen_route, context=context, bc_id=bc_id), route
        )
        _access(session, context, job)
        if job.status == "READY" and not _fresh(job):
            _queue(session, job, read=True)
        return _result(session, job)
    if asset.image_id:
        # 无历史封面job的既有平台图片沿原验证事实复用；不能虚构上传job要求原件摘要。
        require_material_route(
            session,
            context=context,
            route=route,
            bc_id=bc_id,
            advertiser_id=advertiser_id,
            capability="read",
        )
        if asset.verified_at < _now() - timedelta(
            seconds=settings.MATERIAL_ASSET_MAX_AGE_SECONDS
        ):
            return AssetPreparation(state="blocked", reason_code="cover_evidence_stale")
        return AssetPreparation(state="ready", mapping=asset_public(asset))
    identity = uuid4()
    job = MaterialCoverJob(
        id=identity,
        tenant_id=context.tenant_id,
        bc_id=bc_id,
        material_id=material_id,
        asset_id=asset.id,
        advertiser_id=advertiser_id,
        connection_id=route.connection_id,
        frozen_route=route.model_dump(mode="json"),
        actor_id=context.actor_id,
        video_id=asset.video_id,
        video_md5=material.video_md5,
        remote_name=f"cover-{identity.hex}.jpg",
    )
    _access(session, context, job, upload=True)
    session.add(job)
    session.flush()
    _queue(session, job, read=False)
    return _result(session, job)


def _job(
    session: Session, context: TenantContext, identity: UUID, *, lock: bool = False
) -> MaterialCoverJob:
    statement = (
        select(MaterialCoverJob)
        .where(
            MaterialCoverJob.tenant_id == context.tenant_id,
            MaterialCoverJob.id == identity,
        )
        .execution_options(populate_existing=True)
    )
    if lock:
        statement = statement.with_for_update()
    job = session.exec(statement).first()
    if job is None:
        raise DomainError("material_not_found", "未找到当前租户封面任务")
    return job


def get_cover_status(
    session: Session, *, context: TenantContext, job_id: UUID
) -> AssetPreparation:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    job = _job(session, context, job_id)
    require_material_route(
        session,
        context=context,
        route=load_material_route(
            job.frozen_route,
            context=context,
            bc_id=job.bc_id,
            connection_id=job.connection_id,
        ),
        bc_id=job.bc_id,
        advertiser_id=job.advertiser_id,
        capability="read",
    )
    return _result(session, job)


def request_cover_reconciliation(
    session: Session, *, context: TenantContext, job_id: UUID
) -> AssetPreparation:
    job = _job(session, context, job_id, lock=True)
    _access(session, context, job)
    _access(
        session,
        TenantContext(tenant_id=job.tenant_id, actor_id=job.actor_id, role="operator"),
        job,
    )
    if job.error_code == "cover_receipt_ambiguous":
        # 已观察到互斥回执是持久冲突；普通只读重试不能抹掉这一事实。
        return _result(session, job)
    if job.status == "READY" and not _fresh(job):
        # A delayed build may resume after its positive image evidence expires.
        # Revalidate the known ID, preserving the original upload identity.
        _queue(session, job, read=True)
        return _result(session, job)
    if (
        job.status in {"READY", "PENDING", "PREPARING", "VERIFYING"}
        or job.request_armed_at is None
    ):
        return _result(session, job)
    job.search_round, job.next_page, job.search_total = uuid4(), 1, None
    job.candidate_image_id, job.search_ambiguous = None, False
    job.failure_count = 0
    _queue(session, job, read=True)
    return _result(session, job)


def request_cover_retry(
    session: Session, *, context: TenantContext, job_id: UUID
) -> AssetPreparation:
    job = _job(session, context, job_id, lock=True)
    _access(session, context, job, upload=True)
    _access(
        session,
        TenantContext(tenant_id=job.tenant_id, actor_id=job.actor_id, role="operator"),
        job,
        upload=True,
    )
    has_receipt = (
        session.exec(
            select(MaterialCoverReceipt.id)
            .where(MaterialCoverReceipt.job_id == job.id)
            .limit(1)
        ).first()
        is not None
    )
    if (
        job.status != "BLOCKED"
        or job.request_armed_at is not None
        or job.known_image_id is not None
        or has_receipt
        or job.dispatch_id is not None
        or (job.claimed_until and job.claimed_until > _now())
    ):
        raise DomainError("cover_retry_forbidden", "已发送或正在处理的封面任务只能核查")
    job.error_code = None
    job.failure_count = 0
    _queue(session, job, read=False)
    return _result(session, job)


def _fenced(
    session: Session, context: TenantContext, identity: UUID, nonce: UUID
) -> MaterialCoverJob | None:
    job = _job(session, context, identity, lock=True)
    if job.claim_token != nonce or not job.claimed_until or job.claimed_until <= _now():
        return None
    return job


def _stop(job: MaterialCoverJob, code: str, *, unknown: bool) -> None:
    job.status = "UNKNOWN" if unknown else "BLOCKED"
    if job.error_code != "cover_receipt_ambiguous":
        job.error_code = code
    job.claim_token = job.claimed_until = job.dispatch_id = None
    job.updated_at = _now()


def _claim(
    database_engine: Engine,
    context: TenantContext,
    job_id: UUID,
    dispatch_id: UUID,
    revision: int,
    *,
    read: bool,
) -> tuple[MaterialCoverJob, UUID] | None:
    with Session(database_engine) as session, session.begin():
        job = _job(session, context, job_id, lock=True)
        if job.error_code == "cover_receipt_ambiguous":
            _stop(job, "cover_receipt_ambiguous", unknown=True)
            return None
        if (
            job.actor_id != context.actor_id
            or job.dispatch_id != dispatch_id
            or job.revision != revision
            or job.status not in {"PENDING", "PREPARING", "VERIFYING"}
        ):
            return None
        dispatch = session.get(PendingDispatch, dispatch_id)
        expected_task = "materials.verify_cover" if read else "materials.prepare_cover"
        if dispatch is None or (
            dispatch.tenant_id != job.tenant_id
            or dispatch.actor_id != job.actor_id
            or dispatch.task_name != expected_task
            or dispatch.task_key != f"cover:{job.id}:{job.revision}"
            or dispatch.payload != {"job_id": str(job.id), "revision": job.revision}
        ):
            return None
        if job.claimed_until and job.claimed_until > _now():
            return None
        if job.request_armed_at is not None and not read:
            _queue(session, job, read=True)
            return None
        try:
            _access(session, context, job, upload=not read)
        except DomainError as error:
            _stop(job, error.code, unknown=bool(job.request_armed_at))
            return None
        nonce = uuid4()
        job.claim_token, job.claimed_until = (
            nonce,
            _now() + timedelta(seconds=CLAIM_SECONDS),
        )
        job.status = "VERIFYING" if read else "PREPARING"
        job.repair_after = job.claimed_until
        session.flush()
        session.expunge(job)
        return job, nonce


def _call[T](
    database_engine: Engine,
    redis_client: Redis,
    context: TenantContext,
    job: MaterialCoverJob,
    nonce: UUID,
    operation: str,
    invoke: Callable[
        [material_types.MaterialOperations, material_types.RemoteCallBudget], T
    ],
    *,
    deadline: datetime,
    arm: bool = False,
    receipt: Callable[[T], None] | None = None,
) -> T:
    policy = admission_policy(operation)
    budget = material_types.RemoteCallBudget(deadline, HARD_LIMIT, policy.lease_ms)
    budget.timeout(upload=arm)
    route = load_material_route(
        job.frozen_route,
        context=context,
        bc_id=job.bc_id,
        connection_id=job.connection_id,
    )

    def check_current() -> None:
        # 协议、读取与上传的每个物理发送都检查当前claim和原目标；事务结束才出网。
        with bounded_session(database_engine, task_deadline=deadline) as db, db.begin():
            current = _fenced(db, context, job.id, nonce)
            if current is None:
                raise DomainError("cover_claim_lost", "封面任务执行权已变化")
            _access(db, context, current, upload=arm)
            budget.timeout(upload=arm)

    check_current()
    with open_tiktok_gateway(
        database_engine=database_engine,
        redis_client=redis_client,
        context=context,
        route=route,
        task_deadline=deadline,
        before_request=check_current,
    ) as gateway:
        if arm:
            with (
                bounded_session(database_engine, task_deadline=deadline) as db,
                db.begin(),
            ):
                current = _fenced(db, context, job.id, nonce)
                if current is None:
                    raise DomainError("cover_claim_lost", "封面任务执行权已变化")
                _access(db, context, current, upload=True)
                if current.request_armed_at is not None:
                    raise DomainError("cover_result_unknown", "原上传结果需要核查")
                current.request_armed_at = _now()
        try:
            result = invoke(gateway.materials, budget)
        except Exception as error:
            if arm and (
                isinstance(error, AccountAdmissionDeferred)
                or isinstance(error, RemoteCallError)
                and error.effect == "NOT_SENT"
            ):
                # 仅当前发送者得到可靠未发送证据时解除本次armed；旧claim和未知结果不能回退。
                with (
                    bounded_session(database_engine, task_deadline=deadline) as db,
                    db.begin(),
                ):
                    current = _fenced(db, context, job.id, nonce)
                    if current is not None and current.known_image_id is None:
                        current.request_armed_at = None
            raise
        if receipt:
            receipt(result)  # 实际image_id须先落库，再离开可能失败的HTTP客户端清理。
        return result


def _receipt_conflict(session: Session, job: MaterialCoverJob) -> bool:
    if job.error_code == "cover_receipt_ambiguous":
        return True
    receipts = session.exec(
        select(MaterialCoverReceipt)
        .where(MaterialCoverReceipt.job_id == job.id)
        .limit(2)
    ).all()
    if len(receipts) > 1:
        return True
    if not receipts:
        return False
    receipt = receipts[0]
    facts = receipt.receipt_facts
    if facts is None or (job.known_image_id and job.known_image_id != receipt.image_id):
        return True
    if job.signature and facts["signature"] and job.signature != facts["signature"]:
        return True
    job.known_image_id = receipt.image_id
    if job.signature is None:
        job.signature = facts["signature"]
    return False


def _invalidate_receipt(session: Session, job: MaterialCoverJob) -> None:
    mapping = _mapping(session, job)
    if mapping and mapping.image_id == job.known_image_id:
        mapping.image_id = None
    _stop(job, "cover_receipt_ambiguous", unknown=True)


def _preserve_receipt(
    database_engine: Engine,
    context: TenantContext,
    job_id: UUID,
    value: material_types.ImageReceipt,
) -> None:
    with Session(database_engine) as session, session.begin():
        original = _job(session, context, job_id)
        session.exec(
            select(MaterialFile)
            .where(
                MaterialFile.tenant_id == context.tenant_id,
                MaterialFile.id == original.material_id,
            )
            .with_for_update()
        ).one()
        current = _job(session, context, job_id, lock=True)
        # 迟到的真实回执不依赖旧actor仍有权限；material→job锁和发布事务同序。
        session.exec(
            insert(MaterialCoverReceipt)
            .values(
                id=uuid4(),
                tenant_id=context.tenant_id,
                job_id=job_id,
                image_id=value.image_id,
                receipt_facts={"signature": value.signature},
                observed_at=_now(),
            )
            .on_conflict_do_nothing(index_elements=["job_id", "image_id"])
        )
        saved = session.exec(
            select(MaterialCoverReceipt).where(
                MaterialCoverReceipt.job_id == job_id,
                MaterialCoverReceipt.image_id == value.image_id,
            )
        ).one()
        if saved.receipt_facts != {"signature": value.signature} or _receipt_conflict(
            session, current
        ):
            # 重复回执只能完全幂等；冲突不改写旧证据，立即撤下冲突图片的可用映射。
            _invalidate_receipt(session, current)


def _save_receipt(
    database_engine: Engine,
    context: TenantContext,
    job: MaterialCoverJob,
    nonce: UUID,
    value: material_types.ImageReceipt,
) -> None:
    try:
        with Session(database_engine) as session, session.begin():
            current = _fenced(session, context, job.id, nonce)
            if current is None:
                raise DomainError("cover_claim_lost", "封面回执需补充核查")
            current.known_image_id = value.image_id
            current.signature = value.signature
            _queue(session, current, read=True)
    except Exception:
        _preserve_receipt(database_engine, context, job.id, value)


def _read_result(
    database_engine: Engine,
    context: TenantContext,
    job: MaterialCoverJob,
    nonce: UUID,
    evidence: dict[str, str] | None,
) -> None:
    with Session(database_engine) as session, session.begin():
        session.exec(
            select(MaterialFile)
            .where(
                MaterialFile.tenant_id == context.tenant_id,
                MaterialFile.id == job.material_id,
            )
            .with_for_update()
        ).one()
        current = _fenced(session, context, job.id, nonce)
        if current is None:
            return
        _access(session, context, current)
        if _receipt_conflict(session, current):
            _invalidate_receipt(session, current)
            return
        if (
            evidence is not None
            and current.signature is not None
            and current.signature != evidence["signature"]
        ):
            _invalidate_receipt(session, current)
            return
        if evidence is None:
            _stop(current, "cover_result_unknown", unknown=True)
            return
        mapping = _mapping(session, current)
        assert mapping
        mapping.image_id = evidence["image_id"]
        current.known_image_id = evidence["image_id"]
        current.signature = evidence["signature"]
        current.status, current.error_code = "READY", None
        current.claim_token = current.claimed_until = current.dispatch_id = None
        current.updated_at = _now()


def _search_result(
    database_engine: Engine,
    context: TenantContext,
    job: MaterialCoverJob,
    nonce: UUID,
    rows: list[dict[str, Any]],
    last: bool,
    total: int,
) -> None:
    with Session(database_engine) as session, session.begin():
        current = _fenced(session, context, job.id, nonce)
        if current is None:
            return
        ids = [row["image_id"] for row in rows]
        prior = (
            select(MaterialCoverJobPage.id)
            .where(
                MaterialCoverJobPage.job_id == current.id,
                MaterialCoverJobPage.search_round == current.search_round,
                col(MaterialCoverJobPage.image_ids).op("?|")(array(ids)),
            )
            .exists()
        )
        repeated = bool(session.scalar(select(prior))) if ids else False
        if (
            len(ids) != len(set(ids))
            or repeated
            or len(ids) > 100
            or total >= 10000
            or (current.search_total is not None and current.search_total != total)
        ):
            _stop(current, "cover_search_incomplete", unknown=True)
            return
        current.search_total = total
        session.add(
            MaterialCoverJobPage(
                tenant_id=current.tenant_id,
                job_id=current.id,
                search_round=current.search_round,
                page=current.next_page,
                total=total,
                image_ids=ids,
            )
        )
        for row in rows:
            if row.get("file_name") == current.remote_name:
                if (
                    current.candidate_image_id
                    and current.candidate_image_id != row["image_id"]
                ):
                    current.search_ambiguous = True
                current.candidate_image_id = row["image_id"]
        if not last:
            current.next_page += 1
            _queue(session, current, read=True)
            return
        session.flush()
        observed = session.exec(
            select(
                func.coalesce(
                    func.sum(func.jsonb_array_length(MaterialCoverJobPage.image_ids)), 0
                )
            ).where(
                MaterialCoverJobPage.job_id == current.id,
                MaterialCoverJobPage.search_round == current.search_round,
            )
        ).one()
        if (
            observed != total
            or not current.candidate_image_id
            or current.search_ambiguous
        ):
            _stop(current, "cover_result_unknown", unknown=True)
            return
        current.known_image_id = current.candidate_image_id
        _queue(session, current, read=True)


def run_cover(
    *,
    database_engine: Engine,
    redis_client: Redis,
    context: TenantContext,
    job_id: UUID,
    dispatch_id: UUID,
    revision: int,
    read: bool,
) -> None:
    deadline = _now() + timedelta(seconds=HARD_LIMIT - 5)
    claimed = _claim(database_engine, context, job_id, dispatch_id, revision, read=read)
    if claimed is None:
        return
    job, nonce = claimed
    try:
        if not read:
            if job.video_md5 is None:
                raise DomainError("cover_video_unverified", "视频签名尚未核实")
            md5 = job.video_md5
            video = _call(
                database_engine,
                redis_client,
                context,
                job,
                nonce,
                "materials.get_videos",
                lambda client, budget: client.read_video_cover(
                    advertiser_id=job.advertiser_id,
                    video_id=job.video_id,
                    md5=md5,
                    budget=budget,
                ),
                deadline=deadline,
            )
            url = video.url
            if not url:
                suggestion = _call(
                    database_engine,
                    redis_client,
                    context,
                    job,
                    nonce,
                    "materials.get_suggested_covers",
                    lambda client, budget: client.suggest_cover(
                        advertiser_id=job.advertiser_id,
                        video_id=job.video_id,
                        width=video.width,
                        height=video.height,
                        budget=budget,
                    ),
                    deadline=deadline,
                )
                url = suggestion.url if suggestion else None
            if not url:
                raise DomainError("cover_unavailable", "平台尚未提供可用的视频封面")
            with Session(database_engine) as session, session.begin():
                current = _fenced(session, context, job.id, nonce)
                if current is None:
                    return
                current.width, current.height = video.width, video.height
            _call(
                database_engine,
                redis_client,
                context,
                job,
                nonce,
                "materials.upload_image_url",
                lambda client, budget: client.upload_image_url(
                    material_types.URLImageUpload(
                        job.advertiser_id, url, job.remote_name
                    ),
                    budget=budget,
                ),
                deadline=deadline,
                arm=True,
                receipt=lambda value: _save_receipt(
                    database_engine, context, job, nonce, value
                ),
            )
            return
        with Session(database_engine) as session, session.begin():
            # 与入口、最终发布和迟到回执同序，不能持job锁反向等asset/material。
            session.exec(
                select(MaterialFile)
                .where(
                    MaterialFile.tenant_id == context.tenant_id,
                    MaterialFile.id == job.material_id,
                )
                .with_for_update()
            ).one()
            current = _fenced(session, context, job.id, nonce)
            if current is None:
                return
            if _receipt_conflict(session, current):
                _invalidate_receipt(session, current)
                return
            job.known_image_id = current.known_image_id
            job.signature = current.signature
        if job.known_image_id:
            image_id = job.known_image_id
            data = _call(
                database_engine,
                redis_client,
                context,
                job,
                nonce,
                "materials.get_images",
                lambda client, budget: client.read_image(
                    advertiser_id=job.advertiser_id, image_id=image_id, budget=budget
                ),
                deadline=deadline,
            )
            evidence = api.verified_image(
                {"list": [api.image_record_data(data)] if data else []},
                image_id=image_id,
                remote_name=job.remote_name,
                signature=job.signature,
                width=job.width,
                height=job.height,
            )
            _read_result(database_engine, context, job, nonce, evidence)
        else:
            page_data = _call(
                database_engine,
                redis_client,
                context,
                job,
                nonce,
                "materials.search_images",
                lambda client, budget: client.search_images(
                    advertiser_id=job.advertiser_id, page=job.next_page, budget=budget
                ),
                deadline=deadline,
            )
            rows, last, total = api.image_search_page(
                api.image_page_data(page_data), page=job.next_page
            )
            _search_result(database_engine, context, job, nonce, rows, last, total)
    except AccountAdmissionDeferred as error:
        with Session(database_engine) as session, session.begin():
            current = _fenced(session, context, job.id, nonce)
            if current:
                _queue(
                    session,
                    current,
                    read=read,
                    delay=max(1, ceil(error.retry_after_ms / 1000)),
                )
    except Exception as error:
        with Session(database_engine) as session, session.begin():
            current = _fenced(session, context, job.id, nonce)
            if current is None:
                return
            code = (
                error.code if isinstance(error, DomainError) else "cover_response_error"
            )
            current.failure_count += 1
            if current.request_armed_at and not read:
                current.error_code = "cover_result_unknown"
                _queue(session, current, read=True)
            else:
                _stop(current, code, unknown=bool(current.request_armed_at))


def repair_cover_dispatches(session: Session, *, limit: int = 100) -> int:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Cover repair limit must be between 1 and 100")
    jobs = session.exec(
        select(MaterialCoverJob)
        .where(
            col(MaterialCoverJob.status).in_(["PENDING", "PREPARING", "VERIFYING"]),
            MaterialCoverJob.repair_after <= _now(),
            or_(
                col(MaterialCoverJob.claimed_until).is_(None),
                col(MaterialCoverJob.claimed_until) <= _now(),
            ),
        )
        .order_by(col(MaterialCoverJob.repair_after), col(MaterialCoverJob.id))
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    count = 0
    now = _now()
    for job in jobs:
        dispatch = session.exec(
            select(PendingDispatch)
            .where(PendingDispatch.id == job.dispatch_id)
            .with_for_update()
        ).first()
        read = bool(job.request_armed_at) or job.status == "VERIFYING"
        expected_task = "materials.verify_cover" if read else "materials.prepare_cover"
        if dispatch is not None and (
            dispatch.tenant_id != job.tenant_id
            or dispatch.actor_id != job.actor_id
            or dispatch.task_name
            not in {"materials.prepare_cover", "materials.verify_cover"}
            or dispatch.task_key != f"cover:{job.id}:{job.revision}"
            or dispatch.payload != {"job_id": str(job.id), "revision": job.revision}
        ):
            _stop(job, "dispatch_payload_invalid", unknown=bool(job.request_armed_at))
        elif dispatch is None or dispatch.task_name != expected_task:
            # A dead armed PREPARE can only advance to a new read-only phase.
            if job.request_armed_at:
                job.error_code = "cover_result_unknown"
            _queue(session, job, read=read)
        else:
            job.claim_token = job.claimed_until = None
            if (
                dispatch.published_at is not None
                and dispatch.published_at <= now - timedelta(seconds=CLAIM_SECONDS)
            ):
                dispatch.published_at = None
            # Preserve the broker's pending identity, attempts and backoff.
            job.repair_after = max(
                now, dispatch.available_at, dispatch.published_at or now
            ) + timedelta(seconds=CLAIM_SECONDS)
        count += 1
    return count
