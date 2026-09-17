"""源封面到目标账户的 IMAGE 独立批次；发送后只能读回实际目标图片。"""

import json
import re
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any
from typing import cast as type_cast
from uuid import UUID

from redis import Redis
from sqlalchemy import Engine, String, and_, cast, func, or_, text
from sqlalchemy.orm import Session as SASession
from sqlmodel import Session, col, select
from sqlmodel.sql.expression import SelectOfScalar

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.bounded_resources import bounded_session
from app.integrations.tiktok.contracts import materials as types
from app.integrations.tiktok.contracts.common import TRANSIENT_NOT_SENT, RemoteCallError
from app.integrations.tiktok.gateway import open_tiktok_gateway
from app.integrations.tiktok.sdk import AccountAdmissionDeferred
from app.jobs.admission import admission_policy

from . import cover_sdk as api
from . import covers
from .batch_distribution import rectangle
from .content_identity import content_key, material_content_key_expression
from .cover_models import MaterialCoverJob, MaterialCoverShareBatch
from .models import AccountMaterial, MaterialAssetOperation, MaterialFile
from .routes import load_material_route, require_material_route


def _content_material_ids(
    db: Session, context: TenantContext, target: MaterialCoverJob
) -> SelectOfScalar[UUID]:
    material = db.get(MaterialFile, target.material_id, populate_existing=True)
    if (
        material is None
        or material.tenant_id != context.tenant_id
        or material.video_md5 != target.video_md5
    ):
        raise DomainError("cover_video_changed", "目标视频内容身份已变化")
    # 只合并已核实的 SHA/MD5/大小；未知摘要仍按素材 ID 独立，不能按名字猜来源。
    return select(MaterialFile.id).where(
        MaterialFile.tenant_id == context.tenant_id,
        material_content_key_expression() == content_key(material),
    )


def _source(
    db: Session,
    context: TenantContext,
    target: MaterialCoverJob,
    *,
    checks: covers._CoverAccessChecks | None = None,
) -> MaterialCoverJob | None:
    # 历史实际上传的 READY 图片也属于有效源；共享得到的目标图片不冒充所有者。
    rows = db.exec(
        select(MaterialCoverJob)
        .where(
            MaterialCoverJob.tenant_id == target.tenant_id,
            MaterialCoverJob.bc_id == target.bc_id,
            col(MaterialCoverJob.material_id).in_(
                _content_material_ids(db, context, target)
            ),
            MaterialCoverJob.video_md5 == target.video_md5,
            MaterialCoverJob.id != target.id,
            MaterialCoverJob.status == "READY",
            or_(
                col(MaterialCoverJob.request_armed_at).is_not(None),
                col(MaterialCoverJob.purpose) == "SOURCE",
            ),
            or_(
                col(MaterialCoverJob.known_image_id).is_not(None),
                col(MaterialCoverJob.candidate_image_id).is_not(None),
            ),
            col(MaterialCoverJob.share_batch_id).is_(None),
        )
        .order_by(col(MaterialCoverJob.updated_at).desc(), col(MaterialCoverJob.id))
        .limit(20)
    ).all()
    for source in rows:
        if (
            covers.verified_cover_image_id(source) is None
            or not api._signature(source.signature)
            or not api._dimension(source.width)
            or not api._dimension(source.height)
        ):
            continue
        try:
            covers._access(db, context, source, checks=checks)
            route = load_material_route(
                target.frozen_route, context=context, bc_id=target.bc_id
            )
            require_material_route(
                db,
                context=context,
                route=route,
                bc_id=target.bc_id,
                advertiser_id=source.advertiser_id,
                capability="upload",
            )
        except DomainError:
            continue
        return source
    return None


def _snapshot(job: MaterialCoverJob, source: MaterialCoverJob) -> dict[str, Any]:
    return {
        "job_id": str(job.id),
        "material_id": str(job.material_id),
        "asset_id": str(job.asset_id),
        "advertiser_id": job.advertiser_id,
        "video_id": job.video_id,
        "video_md5": job.video_md5,
        "source_job_id": str(source.id),
        "source_video_id": source.video_id,
        "source_image_id": covers.verified_cover_image_id(source),
        "signature": source.signature,
        "width": source.width,
        "height": source.height,
        "file_name": source.remote_name,
    }


def _required[T](db: Session, model: type[T], identity: UUID) -> T:
    row = db.get(model, identity)
    if row is None:
        raise DomainError("cover_claim_lost", "封面持久身份已变化")
    return row


def _start_recorded_source(
    db: Session, context: TenantContext, target: MaterialCoverJob
) -> bool:
    """旧库存或成功事件尚未消费时，从真实成功上传位置补排源封面。"""
    remote = col(MaterialAssetOperation.remote_response)
    receipt_vid_sql = func.nullif(remote["upload_video_id"].as_string(), "")
    verified_vid_sql = func.nullif(remote["verified_upload_video_id"].as_string(), "")
    operations = db.exec(
        select(MaterialAssetOperation)
        .join(
            AccountMaterial,
            and_(
                col(AccountMaterial.tenant_id) == col(MaterialAssetOperation.tenant_id),
                col(AccountMaterial.bc_id) == col(MaterialAssetOperation.bc_id),
                col(AccountMaterial.material_id)
                == col(MaterialAssetOperation.material_id),
                col(AccountMaterial.advertiser_id)
                == col(MaterialAssetOperation.advertiser_id),
                col(AccountMaterial.video_id) == remote["video_id"].as_string(),
                cast(col(AccountMaterial.connection_id), String)
                == col(MaterialAssetOperation.frozen_route)[
                    "connection_id"
                ].as_string(),
            ),
        )
        .where(
            MaterialAssetOperation.tenant_id == context.tenant_id,
            MaterialAssetOperation.bc_id == target.bc_id,
            col(MaterialAssetOperation.material_id).in_(
                _content_material_ids(db, context, target)
            ),
            col(AccountMaterial.status) == "available",
            col(AccountMaterial.verified_at).is_not(None),
            func.length(func.trim(col(AccountMaterial.video_id))) > 0,
            # 先排除不属于当前实际映射的历史及无所有权证据的派生，再限制候选数量。
            or_(
                col(MaterialAssetOperation.path) == "upload_original",
                and_(
                    col(MaterialAssetOperation.path) == "share_source",
                    remote["transport"].as_string() == "url_relay",
                    remote["content_md5"].as_string() == target.video_md5,
                    func.coalesce(remote["conflicting_video_id"].as_string(), "") == "",
                    func.coalesce(receipt_vid_sql, verified_vid_sql)
                    == col(AccountMaterial.video_id),
                    or_(
                        receipt_vid_sql.is_(None),
                        verified_vid_sql.is_(None),
                        receipt_vid_sql == verified_vid_sql,
                    ),
                ),
            ),
            MaterialAssetOperation.status == "succeeded",
        )
        .order_by(col(MaterialAssetOperation.id))
        .limit(20)
    ).all()
    for operation in operations:
        evidence = operation.remote_response
        receipt_vid = evidence.get("upload_video_id")
        verified_vid = evidence.get("verified_upload_video_id")
        owned_vid = receipt_vid or verified_vid
        if operation.path == "share_source" and (
            evidence.get("transport") != "url_relay"
            or not owned_vid
            or owned_vid != evidence.get("video_id")
            or (receipt_vid and verified_vid and receipt_vid != verified_vid)
            or evidence.get("content_md5") != target.video_md5
            or evidence.get("conflicting_video_id")
        ):
            # 上传原回执和丢回执后的严格读回分开取证；有矛盾不能以其中之一覆盖另一份。
            # 跨 BC URL relay 是实际上传；native_share 或不匹配的回执不证明所有权。
            continue
        asset = db.exec(
            select(AccountMaterial).where(
                AccountMaterial.tenant_id == context.tenant_id,
                AccountMaterial.bc_id == target.bc_id,
                AccountMaterial.material_id == operation.material_id,
                AccountMaterial.advertiser_id == operation.advertiser_id,
                AccountMaterial.video_id == operation.remote_response.get("video_id"),
                AccountMaterial.status == "available",
            )
        ).first()
        if asset is None:
            continue
        route = load_material_route(
            operation.frozen_route, context=context, bc_id=target.bc_id
        )
        if asset.connection_id != route.connection_id:
            continue
        result = covers.ensure_source_cover(
            db,
            context=context,
            bc_id=target.bc_id,
            material_id=operation.material_id,
            advertiser_id=asset.advertiser_id,
            task_key=f"cover-source:{operation.id}",
            route=route,
        )
        return result.state in {"queued", "ready"}
    return False


def _prepare(
    db: Session, context: TenantContext, first: MaterialCoverJob, nonce: UUID
) -> tuple[MaterialCoverShareBatch, dict[UUID, UUID]] | None:
    # 同固定授权只锁领取事务，远端调用完全在事务外运行。
    identity = (
        str(first.tenant_id),
        first.bc_id,
        str(first.actor_id),
        first.frozen_route,
    )
    lock = int.from_bytes(
        sha256(json.dumps(identity, sort_keys=True).encode()).digest()[:8],
        "big",
        signed=True,
    )
    type_cast(SASession, db).execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock}
    )
    anchor = covers._job(db, context, first.id)
    if (
        anchor.claim_token != nonce
        or not anchor.claimed_until
        or anchor.claimed_until <= covers._now()
    ):
        raise DomainError("cover_claim_lost", "封面执行权已变化")
    source = _source(db, context, anchor)
    if source is None:
        db.exec(
            select(MaterialFile)
            .where(
                MaterialFile.tenant_id == context.tenant_id,
                col(MaterialFile.id).in_(_content_material_ids(db, context, anchor)),
            )
            .order_by(col(MaterialFile.id))
            .with_for_update()
        ).all()
        locked_anchor = covers._fenced(db, context, first.id, nonce)
        if locked_anchor is None:
            raise DomainError("cover_claim_lost", "封面执行权已变化")
        anchor = locked_anchor
        started = _start_recorded_source(db, context, anchor)
        preparing = db.exec(
            select(MaterialCoverJob.id)
            .where(
                MaterialCoverJob.tenant_id == first.tenant_id,
                MaterialCoverJob.bc_id == first.bc_id,
                col(MaterialCoverJob.material_id).in_(
                    _content_material_ids(db, context, first)
                ),
                MaterialCoverJob.purpose == "SOURCE",
                col(MaterialCoverJob.status).in_(["PENDING", "PREPARING", "VERIFYING"]),
            )
            .limit(1)
        ).first()
        if preparing or started:
            # 自动提升可能沿用持有图片候选的当前 job；续跑不能绕过库存只读核查。
            covers._queue(
                db,
                anchor,
                read=bool(
                    anchor.request_armed_at
                    or anchor.known_image_id
                    or anchor.candidate_image_id
                ),
                delay=5,
            )
        else:
            covers._stop(anchor, "cover_source_unavailable", unknown=False)
        return None
    conditions = (
        MaterialCoverJob.tenant_id == first.tenant_id,
        MaterialCoverJob.bc_id == first.bc_id,
        MaterialCoverJob.actor_id == first.actor_id,
        MaterialCoverJob.connection_id == first.connection_id,
        MaterialCoverJob.frozen_route == first.frozen_route,
        MaterialCoverJob.purpose == "BUILD",
        MaterialCoverJob.status == "PENDING",
        col(MaterialCoverJob.share_batch_id).is_(None),
        col(MaterialCoverJob.request_armed_at).is_(None),
        col(MaterialCoverJob.known_image_id).is_(None),
    )
    # 一次远端共享最多20项×10账户。只检查包含当前锚点的一组候选；
    # 其余任务保留原投递，避免积压越大、每次领取的本地SQL越多而永远超时。
    material_ids = (
        select(MaterialCoverJob.material_id)
        .where(*conditions)
        .where(MaterialCoverJob.material_id != anchor.material_id)
        .distinct()
        .order_by(col(MaterialCoverJob.material_id))
        .limit(19)
    )
    advertisers = (
        select(MaterialCoverJob.advertiser_id)
        .where(*conditions)
        .where(MaterialCoverJob.advertiser_id != anchor.advertiser_id)
        .distinct()
        .order_by(col(MaterialCoverJob.advertiser_id))
        .limit(9)
    )
    rows = db.exec(
        select(MaterialCoverJob)
        .where(*conditions)
        .where(
            or_(
                col(MaterialCoverJob.material_id) == anchor.material_id,
                col(MaterialCoverJob.material_id).in_(material_ids),
            ),
            or_(
                col(MaterialCoverJob.advertiser_id) == anchor.advertiser_id,
                col(MaterialCoverJob.advertiser_id).in_(advertisers),
            ),
        )
        .order_by(
            col(MaterialCoverJob.material_id), col(MaterialCoverJob.advertiser_id)
        )
        .limit(200)
    ).all()
    checks = covers._CoverAccessChecks(db, context)
    # 同一平台图片可属于多个本地视频；矩形领取按本地素材保留全部目标 job。
    pairs = {(str(anchor.material_id), anchor.advertiser_id): (anchor, source)}
    sources: dict[UUID, MaterialCoverJob | None] = {anchor.material_id: source}
    for job in rows:
        if job.material_id not in sources:
            sources[job.material_id] = _source(db, context, job, checks=checks)
        candidate = sources[job.material_id]
        if candidate and (candidate.advertiser_id, candidate.frozen_route) == (
            source.advertiser_id,
            source.frozen_route,
        ):
            pairs[(str(job.material_id), job.advertiser_id)] = (job, candidate)
    selected = rectangle(set(pairs), (str(anchor.material_id), anchor.advertiser_id))
    db.exec(
        select(MaterialFile)
        .where(
            MaterialFile.tenant_id == context.tenant_id,
            col(MaterialFile.id).in_(
                {job.material_id for pair in selected for job in pairs[pair]}
            ),
        )
        .order_by(col(MaterialFile.id))
        .with_for_update()
    ).all()
    if covers._fenced(db, context, first.id, nonce) is None:
        raise DomainError("cover_claim_lost", "封面执行权已变化")
    claims = {first.id: nonce}
    members = []
    for pair in sorted(selected):
        job, selected_source = pairs[pair]
        if job.id != first.id:
            if not job.dispatch_id:
                raise DomainError("cover_claim_lost", "封面任务缺少调度身份")
            claimed = covers._claim_in_session(
                db,
                context,
                job.id,
                job.dispatch_id,
                job.revision,
                read=False,
                checks=checks,
            )
            if claimed is None:
                # 事务回滚后重新领取，不能发送缺成员的原矩形。
                raise DomainError("cover_claim_lost", "封面成员已被领取")
            claims[job.id] = claimed[1]
        members.append(_snapshot(job, selected_source))
    batch = MaterialCoverShareBatch(
        tenant_id=first.tenant_id,
        bc_id=first.bc_id,
        actor_id=first.actor_id,
        source_advertiser_id=source.advertiser_id,
        source_route=type_cast(dict[str, Any], source.frozen_route),
        target_route=type_cast(dict[str, Any], first.frozen_route),
        members=members,
        wake_job_id=first.id,
    )
    db.add(batch)
    db.flush()
    for member in members:
        job = _required(db, MaterialCoverJob, UUID(member["job_id"]))
        job.share_batch_id = batch.id
        if job.id != first.id:
            job.dispatch_id = None
        job.width, job.height, job.signature = (
            member["width"],
            member["height"],
            member["signature"],
        )
    db.flush()
    db.expunge(batch)
    return batch, claims


def _resume(
    db: Session, context: TenantContext, first: MaterialCoverJob, nonce: UUID
) -> tuple[MaterialCoverShareBatch, dict[UUID, UUID]]:
    batch = db.exec(
        select(MaterialCoverShareBatch)
        .where(
            MaterialCoverShareBatch.id == first.share_batch_id,
            MaterialCoverShareBatch.tenant_id == context.tenant_id,
            MaterialCoverShareBatch.bc_id == first.bc_id,
            MaterialCoverShareBatch.actor_id == context.actor_id,
        )
        .with_for_update()
    ).one()
    if batch.status == "READY":
        # 过期 READY 的显式复核必须重新读取，不能重放上轮已缓存的正向证据。
        batch.scan_state = {}
    claims = {first.id: nonce}
    for member in batch.members:
        identity = UUID(member["job_id"])
        job = db.get(MaterialCoverJob, identity)
        if job is None or job.status == "READY" or identity == first.id:
            continue
        if job.claimed_until and job.claimed_until > covers._now():
            raise DomainError("cover_claim_lost", "共享核查成员正在执行")
        job.claim_token = nonce
        job.claimed_until = covers._now() + timedelta(seconds=covers.CLAIM_SECONDS)
        job.repair_after = job.claimed_until
        job.status = "VERIFYING" if batch.armed_at else "PREPARING"
        claims[identity] = nonce
    db.flush()
    db.expunge(batch)
    return batch, claims


def _continue(
    db: Session,
    context: TenantContext,
    batch: MaterialCoverShareBatch,
    claims: dict[UUID, UUID],
    *,
    delay: int = 0,
) -> None:
    """一个批次仅一条续跑 outbox；成员不独立重扫，也不由 repair 放大调度。"""
    current = _required(db, MaterialCoverShareBatch, batch.id)
    wake = (
        current.wake_job_id
        if current.wake_job_id in claims
        else sorted(claims, key=str)[0]
    )
    current.wake_job_id = wake
    for identity, claim in claims.items():
        job = covers._fenced(db, context, identity, claim)
        if job is None:
            raise DomainError("cover_claim_lost", "共享续跑执行权已变化")
        if identity == wake:
            covers._queue(db, job, read=bool(current.armed_at), delay=delay)
        else:
            job.claim_token = job.claimed_until = job.dispatch_id = None
            job.status = "VERIFYING" if current.armed_at else "PENDING"
            job.repair_after = covers._now() + timedelta(
                seconds=covers.CLAIM_SECONDS + delay
            )


def _check(
    db: Session,
    context: TenantContext,
    batch: MaterialCoverShareBatch,
    claims: dict[UUID, UUID],
    *,
    upload: bool = False,
) -> None:
    # 每次真实请求仍重新读取；同一短事务内批读映射并按账户复用授权核查。
    from app.modules.tenants.permissions import require_tenant

    from .batch_validation import BatchSourceVerifier

    current = db.get(MaterialCoverShareBatch, batch.id, populate_existing=True)
    if current is None or (
        current.tenant_id,
        current.bc_id,
        current.actor_id,
        current.target_route,
        current.source_route,
        current.members,
    ) != (
        batch.tenant_id,
        batch.bc_id,
        batch.actor_id,
        batch.target_route,
        batch.source_route,
        batch.members,
    ):
        raise DomainError("cover_claim_lost", "共享批次范围已变化")
    require_tenant(
        db, actor_id=context.actor_id, tenant_id=context.tenant_id, action="build"
    )
    jobs = {
        row.id: row
        for row in db.exec(
            select(MaterialCoverJob)
            .where(
                MaterialCoverJob.tenant_id == context.tenant_id,
                col(MaterialCoverJob.id).in_(claims),
            )
            .execution_options(populate_existing=True)
        ).all()
    }
    source_ids = {UUID(member["source_job_id"]) for member in batch.members}
    sources = {
        row.id: row
        for row in db.exec(
            select(MaterialCoverJob)
            .where(
                MaterialCoverJob.tenant_id == context.tenant_id,
                col(MaterialCoverJob.id).in_(source_ids),
            )
            .execution_options(populate_existing=True)
        ).all()
    }
    # 批次 BC 约束实际视频/图片位置；共享内容可来自租户内任意原上传 BC。
    materials = db.exec(
        select(MaterialFile)
        .where(
            MaterialFile.tenant_id == context.tenant_id,
            col(MaterialFile.id).in_({job.material_id for job in jobs.values()}),
        )
        .execution_options(populate_existing=True)
    ).all()
    verifier = BatchSourceVerifier(
        db,
        context=context,
        source_bc_id=batch.bc_id,
        source_asset_ids={job.asset_id for job in (*jobs.values(), *sources.values())},
        materials=materials,
    )
    target_route = load_material_route(
        batch.target_route, context=context, bc_id=batch.bc_id
    )
    source_route = load_material_route(
        batch.source_route, context=context, bc_id=batch.bc_id
    )
    now = covers._now()

    def mapping(job: MaterialCoverJob) -> AccountMaterial:
        item = verifier.source(
            db,
            context=context,
            bc_id=batch.bc_id,
            material_id=job.material_id,
            source_asset_id=job.asset_id,
        )
        material = verifier.materials.get(job.material_id)
        if (
            item is None
            or material is None
            or material.video_md5 != job.video_md5
            or (item.advertiser_id, item.video_id, item.connection_id)
            != (job.advertiser_id, job.video_id, job.connection_id)
        ):
            raise DomainError("cover_video_changed", "视频映射或摘要已变化")
        return item

    for member in batch.members:
        identity = UUID(member["job_id"])
        if identity not in claims:
            continue
        job = jobs.get(identity)
        if (
            job is None
            or job.share_batch_id != batch.id
            or job.claim_token != claims[identity]
            or not job.claimed_until
            or job.claimed_until <= now
        ):
            raise DomainError("cover_claim_lost", "共享成员执行权已变化")
        if (
            str(job.material_id),
            str(job.asset_id),
            job.advertiser_id,
            job.video_id,
            job.video_md5,
            job.frozen_route,
        ) != (
            member["material_id"],
            member["asset_id"],
            member["advertiser_id"],
            member["video_id"],
            member["video_md5"],
            batch.target_route,
        ):
            raise DomainError("cover_video_changed", "目标视频或连接已变化")
        mapping(job)
        for capability in ("read", "build", "upload") if upload else ("read", "build"):
            verifier.require_route(
                db,
                context=context,
                route=target_route,
                bc_id=batch.bc_id,
                advertiser_id=job.advertiser_id,
                capability=capability,
            )
        source = sources.get(UUID(member["source_job_id"]))
        if source is None or (
            source.bc_id,
            source.advertiser_id,
            source.video_id,
            source.video_md5,
            source.frozen_route,
            covers.verified_cover_image_id(source),
            source.signature,
            source.width,
            source.height,
        ) != (
            batch.bc_id,
            batch.source_advertiser_id,
            member["source_video_id"],
            member["video_md5"],
            batch.source_route,
            member["source_image_id"],
            member["signature"],
            member["width"],
            member["height"],
        ):
            raise DomainError("cover_source_changed", "源封面证据已变化")
        if mapping(source).image_id != member["source_image_id"]:
            raise DomainError("cover_source_changed", "源图片映射已变化")
        # source_job_id 保留真实来源身份；别名仅共享内容，绝不改写来源 job/operation。
        if content_key(verifier.materials[source.material_id]) != content_key(
            verifier.materials[job.material_id]
        ):
            raise DomainError("cover_source_changed", "源与目标的可信内容身份已变化")
        if member.get("source_mid") and source.image_mid != member["source_mid"]:
            raise DomainError("cover_source_changed", "源图片 MID 已变化")
        verifier.require_route(
            db,
            context=context,
            route=source_route,
            bc_id=batch.bc_id,
            advertiser_id=source.advertiser_id,
            capability="read",
        )
        verifier.require_route(
            db,
            context=context,
            route=target_route,
            bc_id=batch.bc_id,
            advertiser_id=source.advertiser_id,
            capability="upload" if upload else "read",
        )
    verifier.recheck_transaction()


def _scan(
    client: types.MaterialOperations,
    batch: MaterialCoverShareBatch,
    claims: dict[UUID, UUID],
    budget: Callable[[str], types.RemoteCallBudget],
) -> tuple[dict[UUID, dict[str, str]], bool]:
    """每轮至多读一页；持久游标跨任务推进，避免大账户永远超时重头扫描。"""
    targets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for member in batch.members:
        if UUID(member["job_id"]) in claims:
            targets[member["advertiser_id"]].append(member)
    state = json.loads(json.dumps(batch.scan_state))
    for target, members in sorted(targets.items()):
        progress = state.setdefault(
            target, {"page": 1, "seen": [], "found": {}, "done": False}
        )
        if not progress["done"]:
            page_number = progress["page"]
            result = client.search_images(
                advertiser_id=target,
                page=page_number,
                budget=budget("materials.search_images"),
            )
            ids = [row.image_id for row in result.rows]
            repeated = set(ids).intersection(progress["seen"])
            if (
                (repeated and result.total_number is None)
                or len(set(ids)) != len(ids)
                or ("total" in progress and progress["total"] != result.total_number)
            ):
                raise DomainError("cover_search_incomplete", "图片分页范围已变化")
            # 平台按修改时间排序，同总数的相邻页也可能重叠。只累计唯一ID，
            # 达到完整库存计数才允许形成“尚未找到”的结论；不把重复行算成新素材。
            progress["seen"] = sorted(set(progress["seen"]).union(ids))
            progress["total"] = result.total_number
            if (
                result.total_number is not None
                and len(progress["seen"]) > result.total_number
            ):
                raise DomainError("cover_search_incomplete", "图片分页范围已变化")
            for member in members:
                for row in result.rows:
                    if row.signature != member["signature"]:
                        continue
                    evidence = api.verified_image(
                        {"list": [api.image_record_data(row)]},
                        image_id=row.image_id,
                        remote_name=member["file_name"],
                        signature=member["signature"],
                        width=member["width"],
                        height=member["height"],
                    )
                    if evidence:
                        progress["found"][member["job_id"]] = evidence
                        break
            progress["done"] = all(
                member["job_id"] in progress["found"] for member in members
            )
            if not progress["done"] and page_number >= result.total_pages:
                if result.total_number is not None and result.total_number >= 10000:
                    raise DomainError(
                        "cover_search_incomplete", "目标图片超过平台可完整搜索范围"
                    )
                if (
                    result.total_number is not None
                    and len(progress["seen"]) < result.total_number
                ):
                    # 最多三轮补齐缺失ID，仍不完整就保留阻断，绝不反复自动共享。
                    rounds = progress.get("round", 1)
                    if rounds >= 3:
                        raise DomainError(
                            "cover_search_incomplete", "图片库存分页仍不完整"
                        )
                    progress["round"], progress["page"] = rounds + 1, 1
                    break
                progress["done"] = True
            if not progress["done"] and page_number >= 100:
                raise DomainError(
                    "cover_search_incomplete", "目标图片超过平台可完整搜索范围"
                )
            progress["page"] = page_number + 1
            break
    batch.scan_state = state
    done = all(state.get(target, {}).get("done", False) for target in targets)
    found = {
        UUID(identity): evidence
        for progress in state.values()
        for identity, evidence in progress["found"].items()
        if UUID(identity) in claims
    }
    return found, done


def _publish(
    db: Session,
    context: TenantContext,
    batch: MaterialCoverShareBatch,
    claims: dict[UUID, UUID],
    found: dict[UUID, dict[str, str]],
) -> None:
    db.exec(
        select(MaterialFile)
        .where(
            MaterialFile.tenant_id == context.tenant_id,
            or_(
                col(MaterialFile.id).in_(
                    {UUID(member["material_id"]) for member in batch.members}
                ),
                col(MaterialFile.id).in_(
                    select(MaterialCoverJob.material_id).where(
                        MaterialCoverJob.tenant_id == context.tenant_id,
                        col(MaterialCoverJob.id).in_(
                            {UUID(member["source_job_id"]) for member in batch.members}
                        ),
                    )
                ),
            ),
        )
        .order_by(col(MaterialFile.id))
        .with_for_update()
    ).all()
    _check(db, context, batch, claims)
    checks = covers._CoverAccessChecks(db, context)
    for identity, evidence in found.items():
        job = covers._fenced(db, context, identity, claims[identity])
        if job is None:
            raise DomainError("cover_claim_lost", "图片核实发布执行权已变化")
        # 目标图片来自当前账户搜索；此时间表示批次确认，绝不伪造目标图片上传。
        job.request_armed_at = batch.armed_at or covers._now()
        covers._publish_result(db, context, job, evidence, checks=checks)


def run_shared_cover(
    database_engine: Engine,
    redis_client: Redis,
    context: TenantContext,
    first: MaterialCoverJob,
    nonce: UUID,
    *,
    deadline: datetime,
) -> None:
    batch = None
    current: MaterialCoverShareBatch | None
    claims: dict[UUID, UUID] = {first.id: nonce}
    armed_this_turn = False
    try:
        with bounded_session(database_engine, task_deadline=deadline) as db, db.begin():
            prepared = (
                _resume(db, context, first, nonce)
                if first.share_batch_id
                else _prepare(db, context, first, nonce)
            )
        if prepared is None:
            return
        batch, claims = prepared

        def check() -> None:
            with (
                bounded_session(database_engine, task_deadline=deadline) as db,
                db.begin(),
            ):
                _check(db, context, batch, claims)

        def budget(operation: str) -> types.RemoteCallBudget:
            return types.RemoteCallBudget(
                deadline, covers.HARD_LIMIT, admission_policy(operation).lease_ms
            )

        route = load_material_route(
            batch.target_route, context=context, bc_id=batch.bc_id
        )
        with open_tiktok_gateway(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            route=route,
            task_deadline=deadline,
            before_request=check,
        ) as gateway:
            if batch.armed_at is None and not all(
                member.get("source_mid") for member in batch.members
            ):
                # 真正的图片 MID 只能来自源账户详情，不能把 image_id 或本地请求当证据。
                records = gateway.materials.read_images(
                    advertiser_id=batch.source_advertiser_id,
                    image_ids=tuple(
                        sorted({member["source_image_id"] for member in batch.members})
                    ),
                    budget=budget("materials.get_images"),
                )
                by_id = {row.image_id: row for row in records}
                members = []
                for member in batch.members:
                    row = by_id.get(member["source_image_id"])
                    if (
                        row is None
                        or row.signature != member["signature"]
                        or not row.mid
                        or not re.fullmatch(r"[0-9]+", row.mid)
                        or not api.verified_image(
                            {"list": [api.image_record_data(row)]},
                            image_id=row.image_id,
                            remote_name=member["file_name"],
                            signature=member["signature"],
                            width=member["width"],
                            height=member["height"],
                        )
                    ):
                        raise DomainError(
                            "cover_source_mid_missing", "源图片 MID 或内容无法核实"
                        )
                    members.append(
                        {
                            **member,
                            "source_mid": row.mid,
                            "file_name": row.file_name or member["file_name"],
                        }
                    )
                with (
                    bounded_session(database_engine, task_deadline=deadline) as db,
                    db.begin(),
                ):
                    _check(db, context, batch, claims)
                    _required(db, MaterialCoverShareBatch, batch.id).members = members
                    for member in members:
                        _required(
                            db, MaterialCoverJob, UUID(member["source_job_id"])
                        ).image_mid = member["source_mid"]
                batch.members = members
            found, done = _scan(gateway.materials, batch, claims, budget)
            with (
                bounded_session(database_engine, task_deadline=deadline) as db,
                db.begin(),
            ):
                _publish(db, context, batch, claims, found)
                current = _required(db, MaterialCoverShareBatch, batch.id)
                current.scan_state = batch.scan_state
                claims = {
                    identity: claim
                    for identity, claim in claims.items()
                    if identity not in found
                }
                # 发布与恢复身份必须同一提交：任何崩溃点都保留一个可修复的 wake。
                if not claims:
                    current.status, current.scan_state = "READY", {}
                elif not done:
                    _continue(db, context, batch, claims)
                elif batch.armed_at is not None:
                    rejected = {
                        UUID(member["job_id"])
                        for member in batch.members
                        if member.get("source_mid")
                        in batch.failed_infos.get(member["advertiser_id"], [])
                    }
                    for identity, claim in claims.items():
                        job = covers._fenced(db, context, identity, claim)
                        if job:
                            covers._stop(
                                job,
                                "cover_share_rejected"
                                if identity in rejected
                                else "cover_result_unknown",
                                unknown=identity not in rejected,
                            )
                    current.status, current.scan_state = (
                        ("BLOCKED" if set(claims) <= rejected else "UNKNOWN"),
                        {},
                    )
                else:
                    current.wake_job_id = (
                        current.wake_job_id
                        if current.wake_job_id in claims
                        else sorted(claims, key=str)[0]
                    )
            if not claims or not done or batch.armed_at is not None:
                return
            # 已存在目标会使原矩形变稀疏；只发送剩余授权组合中的一个矩形。
            pending = [
                member for member in batch.members if UUID(member["job_id"]) in claims
            ]
            # 主账户本身使用内容别名时只能读回现有图片；库存未证实不能发 self share。
            if any(
                member["advertiser_id"] == batch.source_advertiser_id
                for member in pending
            ):
                raise DomainError(
                    "cover_source_inventory_unverified", "主账户源图片库存尚未核实"
                )
            pairs = {
                (member["source_mid"], member["advertiser_id"]) for member in pending
            }
            chosen = rectangle(pairs, sorted(pairs)[0])
            selected = [
                member
                for member in pending
                if (member["source_mid"], member["advertiser_id"]) in chosen
            ]
            request = types.AssetShare(
                batch.source_advertiser_id,
                tuple(sorted({mid for mid, _ in chosen})),
                tuple(sorted({target for _, target in chosen})),
                "IMAGE",
            )
            with (
                bounded_session(database_engine, task_deadline=deadline) as db,
                db.begin(),
            ):
                _check(db, context, batch, claims, upload=True)
                for member in pending:
                    if member not in selected:
                        job = _required(db, MaterialCoverJob, UUID(member["job_id"]))
                        job.share_batch_id = None
                        covers._queue(db, job, read=False)
                current = _required(db, MaterialCoverShareBatch, batch.id)
                # 已复用 READY 的 job 仍属于真实来源批次，保留只读成员以便过期核查。
                retained = [
                    {**member, "share_requested": member in selected}
                    for member in batch.members
                    if member not in pending or member in selected
                ]
                current.members = retained
                selected_ids = {UUID(member["job_id"]) for member in selected}
                current.wake_job_id = (
                    current.wake_job_id
                    if current.wake_job_id in selected_ids
                    else sorted(selected_ids, key=str)[0]
                )
                current.request_digest = sha256(
                    json.dumps(
                        {
                            "material_ids": request.material_ids,
                            "targets": request.shared_advertiser_ids,
                            "type": "IMAGE",
                        },
                        sort_keys=True,
                    ).encode()
                ).hexdigest()
                current.armed_at, current.status = covers._now(), "SENDING"
                current.scan_state = {}
                for member in selected:
                    _required(
                        db, MaterialCoverJob, UUID(member["job_id"])
                    ).request_armed_at = current.armed_at
                db.flush()
                batch.armed_at, batch.status, batch.members = (
                    current.armed_at,
                    current.status,
                    retained,
                )
            claims = {
                UUID(member["job_id"]): claims[UUID(member["job_id"])]
                for member in selected
            }
            armed_this_turn = True
            receipt = gateway.materials.share_assets(
                request, budget=budget("materials.share_assets")
            )
            with Session(database_engine) as db, db.begin():
                current = _required(db, MaterialCoverShareBatch, batch.id)
                current.failed_infos = {
                    target: list(mids) for target, mids in receipt.failed_infos.items()
                }
                current.request_id = receipt.evidence.request_id
                current.status = "VERIFYING"
                _continue(db, context, batch, claims)
    except Exception as error:
        # 即使清理客户端失败也不能重发已 armed 的批次；先保存读回调度。
        with Session(database_engine) as db, db.begin():
            current = (
                _required(db, MaterialCoverShareBatch, batch.id) if batch else None
            )
            unknown = bool(current and current.armed_at)
            code = (
                error.code if isinstance(error, DomainError) else "cover_share_unknown"
            )
            if current:
                current.status, current.error_code = (
                    ("UNKNOWN" if unknown else "BLOCKED"),
                    code,
                )
            active = {}
            for identity, claim in claims.items():
                job = covers._fenced(db, context, identity, claim)
                if job:
                    if not isinstance(error, AccountAdmissionDeferred):
                        job.failure_count += 1
                    active[identity] = job
            retry = isinstance(error, AccountAdmissionDeferred) or (
                isinstance(error, RemoteCallError)
                and error.code in TRANSIENT_NOT_SENT
                and all(job.failure_count < 3 for job in active.values())
                and (unknown or error.effect == "NOT_SENT")
            )
            if current and active and (armed_this_turn or retry):
                current.status = "UNKNOWN" if unknown else "PREPARING"
                _continue(
                    db,
                    context,
                    current,
                    {identity: claims[identity] for identity in active},
                    delay=5,
                )
                return
            if current:
                current.scan_state = {}
            for job in active.values():
                if retry:
                    covers._queue(db, job, read=False, delay=5)
                else:
                    covers._stop(job, code, unknown=unknown)
