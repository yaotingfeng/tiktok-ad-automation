"""主图资产：复用 outbox/租约/原件预算，上传和共享都必须账户回读。

UNKNOWN 仅执行有界库存核实，不重新创建远端素材；签名 URL 仅保留在调用栈。
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.bounded_resources import bounded_session
from app.integrations.tiktok.contracts.common import RemoteCallError
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.integrations.tiktok.contracts.materials import (
    AssetShare,
    RemoteCallBudget,
    URLImageUpload,
)
from app.integrations.tiktok.gateway import open_tiktok_gateway
from app.jobs.admission import admission_policy
from app.jobs.models import PendingDispatch
from app.modules.tenants.permissions import require_tenant

from .ingest_models import (
    IngestSession,
    IngestSessionFile,
    TemporaryMaterialObject,
    record_milestone,
)
from .models import (
    AccountMaterial,
    MaterialAssetOperation,
    MaterialDistribution,
    MaterialFile,
    MaterialUploadAttempt,
)
from .routes import (
    load_material_route,
    require_material_route,
    require_same_route,
    require_sdk_route,
)
from .source_uploads import READ_HARD_LIMIT, UPLOAD_HARD_LIMIT, _locked_material


@dataclass(frozen=True)
class ImageTarget:
    material_id: UUID
    advertiser_id: str
    state: str
    task_id: UUID | None = None
    asset_id: UUID | None = None


@dataclass(frozen=True)
class ImageDistributionResult:
    items: tuple[ImageTarget, ...]


def image_ready(
    asset: AccountMaterial | None, route: FrozenTikTokRoute | None = None
) -> bool:
    return bool(
        asset
        and asset.media_type == "IMAGE"
        and asset.video_id is None
        and asset.image_id
        and asset.is_carousel_usable is True
        and asset.status == "available"
        and asset.verified_at
        and (route is None or asset.frozen_route == route.model_dump(mode="json"))
    )


def _mapping(db, route, material_id, advertiser_id):
    return db.exec(
        select(AccountMaterial).where(
            AccountMaterial.tenant_id == route.tenant_id,
            AccountMaterial.bc_id == route.bc_id,
            AccountMaterial.material_id == material_id,
            AccountMaterial.advertiser_id == advertiser_id,
        )
    ).first()


def _authorize(db, context, route, account, *, capability="build"):
    require_sdk_route(route)
    require_material_route(
        db,
        context=context,
        route=route,
        bc_id=route.bc_id,
        advertiser_id=account,
        capability=capability,
    )


def prepare_image_targets(
    route: FrozenTikTokRoute,
    material_ids: tuple[UUID, ...],
    advertiser_ids: tuple[str, ...],
    *,
    session: Session,
    context: TenantContext,
) -> ImageDistributionResult:
    """一个事务最多 100 个图片/账户对；上下文由调用方租户服务提供。"""
    ids, accounts = (
        tuple(dict.fromkeys(material_ids)),
        tuple(dict.fromkeys(advertiser_ids)),
    )
    if not ids or not accounts or len(ids) * len(accounts) > 100:
        raise DomainError(
            "image_distribution_limit", "图片分发每次最多 100 个账户素材对"
        )
    results = []
    # 全部素材按相同顺序锁定，同一图片的多个轮播共用唯一账户任务。
    for material_id in sorted(ids):
        material = _locked_material(session, context, material_id)
        if material.media_type != "IMAGE":
            raise DomainError("image_material_required", "图片分发只接受图片原始素材")
        for account in accounts:
            _authorize(session, context, route, account)
            mapping = _mapping(session, route, material_id, account)
            if image_ready(mapping, route):
                results.append(
                    ImageTarget(material_id, account, "ready", asset_id=mapping.id)
                )
                continue
            dist = session.exec(
                select(MaterialDistribution)
                .where(
                    MaterialDistribution.tenant_id == route.tenant_id,
                    MaterialDistribution.bc_id == route.bc_id,
                    MaterialDistribution.material_id == material_id,
                    MaterialDistribution.advertiser_id == account,
                    col(MaterialDistribution.superseded_by_id).is_(None),
                )
                .order_by(col(MaterialDistribution.id))
            ).first()
            if dist:
                require_same_route(
                    load_material_route(
                        dist.target_route, context=context, bc_id=route.bc_id
                    ),
                    route,
                )
            else:
                dist = _create_target(session, context, route, material, account)
            results.append(
                ImageTarget(material_id, account, dist.status, task_id=dist.id)
            )
    return ImageDistributionResult(tuple(results))


def _create_target(db, context, route, material, account, *, operation=None):
    from .distribution import queue_distribution

    source = None
    if operation is None:
        sources = db.exec(
            select(AccountMaterial)
            .where(
                AccountMaterial.tenant_id == route.tenant_id,
                AccountMaterial.bc_id == route.bc_id,
                AccountMaterial.material_id == material.id,
                AccountMaterial.media_type == "IMAGE",
                AccountMaterial.status == "available",
                col(AccountMaterial.is_carousel_usable).is_(True),
                AccountMaterial.advertiser_id != account,
            )
            .order_by(col(AccountMaterial.id))
        ).all()
        for candidate in sources:
            if (
                not image_ready(candidate, route)
                or not candidate.mid
                or candidate.connection_id != route.connection_id
            ):
                continue
            try:
                _authorize(
                    db, context, route, candidate.advertiser_id, capability="read"
                )
            except DomainError:
                continue
            source = candidate
            break
        response = {
            "media_type": "IMAGE",
            "revision": 0,
            "remote_name": material.file_name,
            "send_armed": False,
        }
        if source:
            response.update(
                source_image_id=source.image_id,
                source_mid=source.mid,
                source_advertiser_id=source.advertiser_id,
            )
        else:
            # 原件使用与操作 BC 必须一致；跨 BC 消费先准备该 BC 已授权图片种子。
            if material.bc_id != route.bc_id or not material.original_available:
                raise DomainError(
                    "image_source_unavailable", "当前 BC 尚无可分发的图片来源"
                )
            obj = material.current_object
            if obj is None or obj.status != "verified" or not obj.digest_verified_at:
                raise DomainError("image_source_unavailable", "图片原件尚未验真")
            _authorize(db, context, route, account, capability="upload")
            response.update(object_id=str(obj.id), generation=obj.generation)
        operation = MaterialAssetOperation(
            tenant_id=route.tenant_id,
            bc_id=route.bc_id,
            material_id=material.id,
            advertiser_id=account,
            path="share_source" if source else "upload_original",
            frozen_route=route.model_dump(mode="json"),
            request_digest=sha256(
                f"image:{route}:{material.id}:{account}".encode()
            ).hexdigest(),
            remote_response=response,
        )
        db.add(operation)
        db.flush()
    dist = MaterialDistribution(
        tenant_id=route.tenant_id,
        bc_id=route.bc_id,
        material_id=material.id,
        advertiser_id=account,
        actor_id=context.actor_id,
        operation_id=operation.id,
        target_route=route.model_dump(mode="json"),
        source_route=route.model_dump(mode="json") if source else None,
        source_bc_id=route.bc_id if source else None,
        source_material_id=material.id if source else None,
        source_asset_id=source.id if source else None,
        path=operation.path,
    )
    db.add(dist)
    db.flush()
    queue_distribution(db, dist, operation, kind="prepare")
    return dist


def enqueue_image_original(
    db: Session,
    *,
    context: TenantContext,
    material: MaterialFile,
    obj: TemporaryMaterialObject,
    row: IngestSessionFile,
) -> UUID:
    """验真事务和历史 handoff 恢复共同入口，持锁登记真实平台任务。"""
    from .routes import source_parent_route
    from .source_url_uploads import _new_operation, _proof

    if material.media_type != "IMAGE" or row.media_type != "IMAGE":
        raise DomainError("image_material_required", "图片任务只接受图片原始素材")

    _proof(material, obj, row, upload=True)
    route = source_parent_route(
        db,
        context=context,
        material_id=material.id,
        bc_id=material.bc_id,
        generation=obj.generation,
    )
    require_sdk_route(route)
    if row.dispatch_id:
        return row.dispatch_id
    operation = _new_operation(db, context=context, material=material, obj=obj)
    operation.remote_response = {
        **operation.remote_response,
        "media_type": "IMAGE",
        "source_ingest": True,
    }
    dist = _create_target(
        db, context, route, material, operation.advertiser_id, operation=operation
    )
    message = db.exec(
        select(PendingDispatch).where(
            PendingDispatch.tenant_id == context.tenant_id,
            col(PendingDispatch.payload)["distribution_id"].as_string() == str(dist.id),
        )
    ).one()
    row.dispatch_id = message.id
    return message.id


def repair_image_handoffs(db: Session, *, limit=100) -> int:
    from sqlalchemy import text

    if type(limit) is not int or not 0 < limit <= 100:
        raise DomainError("invalid_asset_task", "图片恢复批量上限为 100")
    db.connection().execute(text("SET LOCAL statement_timeout = '10s'"))
    db.connection().execute(text("SET LOCAL lock_timeout = '2s'"))
    candidates = db.exec(
        select(MaterialFile, TemporaryMaterialObject, IngestSessionFile, IngestSession)
        .join(
            TemporaryMaterialObject,
            (col(TemporaryMaterialObject.material_id) == MaterialFile.id)
            & (col(TemporaryMaterialObject.tenant_id) == MaterialFile.tenant_id)
            & (col(TemporaryMaterialObject.bc_id) == MaterialFile.bc_id)
            & (
                col(TemporaryMaterialObject.generation)
                == MaterialFile.current_object_generation
            ),
        )
        .join(
            IngestSessionFile,
            (col(IngestSessionFile.material_id) == MaterialFile.id)
            & (col(IngestSessionFile.tenant_id) == MaterialFile.tenant_id)
            & (col(IngestSessionFile.bc_id) == MaterialFile.bc_id)
            & (
                col(IngestSessionFile.current_generation)
                == TemporaryMaterialObject.generation
            ),
        )
        .join(
            IngestSession,
            (col(IngestSession.id) == IngestSessionFile.session_id)
            & (col(IngestSession.tenant_id) == MaterialFile.tenant_id),
        )
        .where(
            MaterialFile.media_type == "IMAGE",
            IngestSessionFile.media_type == "IMAGE",
            IngestSessionFile.status == "stored",
            col(IngestSessionFile.dispatch_id).is_(None),
            TemporaryMaterialObject.status == "verified",
            col(TemporaryMaterialObject.digest_verified_at).is_not(None),
        )
        .order_by(col(MaterialFile.id))
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    count = 0
    for material, obj, row, parent in candidates:
        context = TenantContext(
            tenant_id=material.tenant_id, actor_id=parent.actor_id, role="operator"
        )
        try:
            with db.begin_nested():
                enqueue_image_original(
                    db, context=context, material=material, obj=obj, row=row
                )
            count += 1
        except DomainError as error:
            row.error_code = error.code
    return count


def _records(db, context, dist_id):
    dist = db.get(MaterialDistribution, dist_id)
    if dist is None or dist.tenant_id != context.tenant_id:
        raise DomainError("material_not_found", "图片任务不属于当前租户")
    material = _locked_material(db, context, dist.material_id)
    db.refresh(dist, with_for_update=True)
    op = db.get(MaterialAssetOperation, dist.operation_id, with_for_update=True)
    if (
        material.media_type != "IMAGE"
        or op is None
        or op.tenant_id != context.tenant_id
    ):
        raise DomainError("image_material_required", "图片任务身份不一致")
    return material, dist, op


def _authority(db, context, dist, op, *, writing=False):
    require_tenant(
        db,
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        action="upload" if op.remote_response.get("source_ingest") else "build",
    )
    route = load_material_route(dist.target_route, context=context, bc_id=dist.bc_id)
    require_same_route(
        load_material_route(op.frozen_route, context=context, bc_id=dist.bc_id), route
    )
    if not op.remote_response.get("source_ingest"):
        # 搭建目标的权限贯穿执行和回读发布，read/upload 不能代替 build。
        _authorize(db, context, route, dist.advertiser_id, capability="build")
    _authorize(
        db,
        context,
        route,
        dist.advertiser_id,
        capability="upload" if writing and op.path == "upload_original" else "read",
    )
    if op.remote_response.get("object_id"):
        obj = db.get(
            TemporaryMaterialObject, UUID(str(op.remote_response["object_id"]))
        )
        material = db.get(MaterialFile, op.material_id)
        if (
            obj is None
            or material is None
            or (obj.tenant_id, obj.bc_id, obj.material_id, obj.generation)
            != (
                op.tenant_id,
                op.bc_id,
                op.material_id,
                op.remote_response.get("generation"),
            )
            or material.current_object_generation != obj.generation
            or not obj.digest_verified_at
            or (material.sha256, material.video_md5) != (obj.sha256, obj.video_md5)
            or (writing and obj.status != "verified")
        ):
            raise DomainError("object_generation_changed", "图片原件身份或代次已改变")
    if dist.source_asset_id:
        source = db.get(AccountMaterial, dist.source_asset_id)
        if (
            not image_ready(source, route)
            or source.connection_id != route.connection_id
            or source.image_id != op.remote_response.get("source_image_id")
            or source.mid != op.remote_response.get("source_mid")
        ):
            raise DomainError("image_source_unavailable", "图片来源证据已改变")
        _authorize(db, context, route, source.advertiser_id, capability="read")
    return route


def _verified(record, _material, account, identity=None):
    return bool(
        record
        and record.advertiser_id == account
        and (identity is None or record.image_id == identity)
        and record.is_carousel_usable is True
    )


def _source_state(db, context, material, op, status, code=None):
    if not op.remote_response.get("source_ingest"):
        return
    from .source_selection import source_file
    from .source_url_uploads import _state

    row = source_file(
        db, context=context, bc_id=material.bc_id, material_id=material.id
    )
    _state(db, context, row, status, code)
    attempt = db.exec(
        select(MaterialUploadAttempt).where(MaterialUploadAttempt.operation_id == op.id)
    ).one()
    attempt.status = status
    attempt.remote_response = dict(op.remote_response)


def _release_original(db, op):
    # 直接目标上传也签发原件使用许可，结束使用不依赖是否为入库来源任务。
    from .object_uses import release_object_uses

    if op.remote_response.get("object_id"):
        release_object_uses(
            db,
            object_id=UUID(str(op.remote_response["object_id"])),
            purpose="ingest",
            operation_id=op.id,
        )


def _close_unsent(db, context, material, op, code):
    _release_original(db, op)
    if not op.remote_response.get("source_ingest"):
        return
    from .source_selection import release_source_account

    _source_state(db, context, material, op, "failed", code)
    release_source_account(db, context=context, operation=op)


def _finish_ingest(db, context, material, op):
    _release_original(db, op)
    if not op.remote_response.get("source_ingest"):
        return
    from .cleanup import schedule_cleanup
    from .source_selection import release_source_account, source_file
    from .source_url_uploads import _state

    attempt = db.exec(
        select(MaterialUploadAttempt).where(MaterialUploadAttempt.operation_id == op.id)
    ).one()
    attempt.status = "available"
    attempt.remote_response = dict(op.remote_response)
    release_source_account(db, context=context, operation=op)
    obj_id = UUID(op.remote_response["object_id"])
    row = source_file(
        db, context=context, bc_id=material.bc_id, material_id=material.id
    )
    _state(db, context, row, "available")
    record_milestone(
        db,
        tenant_id=context.tenant_id,
        bc_id=material.bc_id,
        session_id=row.session_id,
        material_id=material.id,
        milestone="ready",
    )
    db.flush()
    schedule_cleanup(db, object_id=obj_id, source_receipt_id=op.id)


def run_image_distribution(
    *,
    database_engine: Any,
    redis_client: Any,
    context: TenantContext,
    distribution_id: UUID,
    kind: str,
    operation_id: UUID | None = None,
    revision: int | None = None,
    recovery_claim_id: UUID | None = None,
    s3: Any = None,
    read_only: bool = False,
) -> None:
    from .distribution import (
        _claim_reconciliation,
        _delivery_matches,
        _reconciliation_exhausted,
        _reconciliation_progress,
        _stop_reconciliation,
        queue_distribution,
    )

    if kind not in {"prepare", "verify"} or (read_only and kind != "verify"):
        raise DomainError("invalid_asset_task", "图片任务类型无效")
    now = datetime.now(UTC)
    with Session(database_engine) as db, db.begin():
        material, dist, op = _records(db, context, distribution_id)
        if (
            dist.superseded_by_id
            or op.superseded_by_id
            or dist.status not in {"queued", "preparing", "verifying", "result_unknown"}
            or not _delivery_matches(
                op,
                operation_id=operation_id,
                revision=revision,
                recovery_claim_id=recovery_claim_id,
            )
        ):
            return
        if op.claimed_until and op.claimed_until > now:
            return
        # 只有从未武装的 pending 可发送；硬杀后 sending 也只核实。
        writing = (
            kind == "prepare"
            and not read_only
            and op.status == "pending"
            and not op.remote_response.get("send_armed")
        )
        kind = "prepare" if writing else "verify"
        try:
            route = _authority(db, context, dist, op, writing=writing)
        except DomainError as error:
            dist.status, dist.reason_code = "blocked", error.code
            if not op.remote_response.get("send_armed"):
                op.status = "failed"
                _close_unsent(db, context, material, op, error.code)
            return
        if not writing:
            if _reconciliation_exhausted(op):
                _stop_reconciliation(dist, op)
                return
            _claim_reconciliation(op)
        claim = uuid4()
        hard = UPLOAD_HARD_LIMIT if writing else READ_HARD_LIMIT
        deadline = now + timedelta(seconds=hard)
        op.attempt_token, op.claimed_until = claim, deadline + timedelta(seconds=10)
        op.remote_response = {
            **op.remote_response,
            "revision": op.remote_response.get("revision", 0) + 1,
        }
        dist.status = "preparing" if writing else "verifying"
        _source_state(
            db, context, material, op, "uploading" if writing else "verifying"
        )
        queue_distribution(
            db, dist, op, kind="verify", due=op.claimed_until, claim_id=claim
        )
        work = dict(op.remote_response)
        op_id, account, path = op.id, dist.advertiser_id, op.path
        db.expunge(material)
    sent = False
    receipt = None
    record = None
    page = None
    try:

        def check_current():
            with (
                bounded_session(database_engine, task_deadline=deadline) as db,
                db.begin(),
            ):
                m, d, o = _records(db, context, distribution_id)
                if o.attempt_token != claim or o.superseded_by_id or d.superseded_by_id:
                    raise DomainError("material_claim_changed", "图片任务已被接替")
                _authority(db, context, d, o, writing=writing)
                if work.get("object_id"):
                    obj = db.get(TemporaryMaterialObject, UUID(work["object_id"]))
                    if (
                        obj is None
                        or obj.material_id != m.id
                        or obj.tenant_id != context.tenant_id
                        or obj.generation != work.get("generation")
                        or m.current_object_generation != obj.generation
                    ):
                        raise DomainError(
                            "object_generation_changed", "图片原件代次已改变"
                        )

        def budget(operation):
            policy = admission_policy(operation, channel=route.channel)
            return RemoteCallBudget(deadline, hard, policy.lease_ms)

        check_current()
        if writing and path == "share_source":
            read_deadline = min(
                deadline, datetime.now(UTC) + timedelta(seconds=READ_HARD_LIMIT)
            )
            with open_tiktok_gateway(
                database_engine=database_engine,
                redis_client=redis_client,
                context=context,
                route=route,
                task_deadline=read_deadline,
                before_request=check_current,
            ) as reader:
                source = reader.materials.read_image(
                    advertiser_id=work["source_advertiser_id"],
                    image_id=work["source_image_id"],
                    budget=RemoteCallBudget(
                        read_deadline,
                        READ_HARD_LIMIT,
                        admission_policy(
                            "materials.get_images", channel=route.channel
                        ).lease_ms,
                    ),
                )
                if (
                    not _verified(
                        source,
                        material,
                        work["source_advertiser_id"],
                        work["source_image_id"],
                    )
                    or source is None
                    or source.mid != work["source_mid"]
                ):
                    raise DomainError(
                        "image_source_unavailable", "来源图片不满足轮播资格"
                    )
                work["source_signature"] = source.signature
        with open_tiktok_gateway(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            route=route,
            task_deadline=deadline,
            before_request=check_current,
        ) as gateway:
            if writing:
                # 发送前落库，任何超时/进程退出均不能重新创建。
                with Session(database_engine) as db, db.begin():
                    _, d, o = _records(db, context, distribution_id)
                    if o.attempt_token != claim:
                        return
                    _authority(db, context, d, o, writing=True)
                    o.status = "sending"
                    o.remote_response = {
                        **o.remote_response,
                        "send_armed": True,
                        **(
                            {"source_signature": work["source_signature"]}
                            if work.get("source_signature")
                            else {}
                        ),
                    }
                if path == "share_source":
                    sent = True
                    shared = gateway.materials.share_assets(
                        AssetShare(
                            work["source_advertiser_id"],
                            (work["source_mid"],),
                            (account,),
                            asset_type="IMAGE",
                        ),
                        budget=budget("materials.share_assets"),
                    )
                    if shared.failed_infos:
                        raise DomainError("image_share_failed", "目标账户图片共享失败")
                    receipt = {
                        "image_id": work["source_image_id"],
                        "share_receipt": True,
                    }
                else:
                    from .object_validation import sign_ingest_url

                    url = sign_ingest_url(
                        database_engine=database_engine,
                        context=context,
                        object_id=UUID(work["object_id"]),
                        operation_id=op_id,
                        s3=s3,
                    )
                    sent = True
                    uploaded = gateway.materials.upload_image_url(
                        URLImageUpload(account, url, work["remote_name"]),
                        budget=budget("materials.upload_image_url"),
                    )
                    receipt = {
                        "image_id": uploaded.image_id,
                        "mid": uploaded.mid,
                        "upload_receipt": True,
                    }
                # 先保存实际身份，再核验当前绑定；迟到回执只能留证，不能 READY。
                with Session(database_engine) as db, db.begin():
                    _, d, o = _records(db, context, distribution_id)
                    if o.attempt_token != claim:
                        return
                    o.remote_response = {**o.remote_response, **receipt}
            elif work.get("image_id"):
                record = gateway.materials.read_image(
                    advertiser_id=account,
                    image_id=work["image_id"],
                    budget=budget("materials.get_images"),
                )
            else:
                page = gateway.materials.search_images(
                    advertiser_id=account,
                    page=work.get("search_page", 1),
                    budget=budget("materials.search_images"),
                )
        with Session(database_engine) as db, db.begin():
            material, dist, op = _records(db, context, distribution_id)
            if op.attempt_token != claim:
                return
            _authority(db, context, dist, op)
            if (
                not writing
                and _verified(record, material, account, work.get("image_id"))
                and (
                    not work.get("source_ingest") or (record is not None and record.mid)
                )
            ):
                # 来源须有真实 MID 才可清理原件，否则后续账户无法原生共享。
                assert record is not None
                mapping = _mapping(db, route, material.id, account)
                if mapping is None:
                    mapping = AccountMaterial(
                        tenant_id=context.tenant_id,
                        bc_id=route.bc_id,
                        material_id=material.id,
                        advertiser_id=account,
                        connection_id=route.connection_id,
                        media_type="IMAGE",
                        video_id=None,
                        frozen_route=route.model_dump(mode="json"),
                    )
                    db.add(mapping)
                mapping.image_id, mapping.mid, mapping.is_carousel_usable = (
                    record.image_id,
                    record.mid,
                    True,
                )
                mapping.frozen_route = route.model_dump(mode="json")
                mapping.connection_id, mapping.status, mapping.verified_at = (
                    route.connection_id,
                    "available",
                    datetime.now(UTC),
                )
                op.status, dist.status, dist.reason_code = "succeeded", "ready", None
                op.remote_response = {
                    **op.remote_response,
                    "image_id": record.image_id,
                    "mid": record.mid,
                    "is_carousel_usable": True,
                    "confirmation_source": "image_readback",
                }
                db.flush()
                _finish_ingest(db, context, material, op)
            else:
                op.status, dist.status = "verifying", "verifying"
                if not writing:
                    progress = False
                    if page is not None:
                        matches = {
                            item["image_id"]: item
                            for item in work.get("candidates", [])
                        }
                        for candidate in page.rows:
                            if candidate.signature == (
                                work.get("source_signature") or material.video_md5
                            ) and (
                                work.get("source_signature")
                                or (
                                    candidate.width == material.width
                                    and candidate.height == material.height
                                )
                            ):
                                matches[candidate.image_id] = {
                                    "image_id": candidate.image_id,
                                    "mid": candidate.mid,
                                }
                        last = page.page >= page.total_pages
                        if last and len(matches) == 1:
                            op.remote_response = {
                                **op.remote_response,
                                **next(iter(matches.values())),
                            }
                            progress = True
                        elif last or len(matches) > 1:
                            op.status = dist.status = "result_unknown"
                            op.remote_response = {
                                **op.remote_response,
                                "reconciliation_complete": True,
                                "error_code": "image_reconciliation_ambiguous"
                                if matches
                                else "material_result_pending",
                            }
                        else:
                            op.remote_response = {
                                **op.remote_response,
                                "search_page": page.page + 1,
                                "candidates": list(matches.values()),
                            }
                            progress = True
                    elif record and record.is_carousel_usable is False:
                        op.status, dist.status, dist.reason_code = (
                            "failed",
                            "blocked",
                            "image_not_carousel_usable",
                        )
                        # 精确图片回读已完成，平台不再读取原件；资格失败仍不能发布映射。
                        op.remote_response = {
                            **op.remote_response,
                            "image_read_complete": True,
                            "is_carousel_usable": False,
                        }
                        _release_original(db, op)
                        if op.remote_response.get("source_ingest"):
                            from .source_selection import release_source_account

                            _source_state(
                                db,
                                context,
                                material,
                                op,
                                "failed",
                                "image_not_carousel_usable",
                            )
                            release_source_account(db, context=context, operation=op)
                    elif record is None and work.get("share_receipt"):
                        # 共享后的目标 ID 可能变化；有界搜索仍须在下一轮按目标 ID 回读。
                        op.remote_response = {
                            key: value
                            for key, value in op.remote_response.items()
                            if key != "image_id"
                        }
                        progress = True
                    _reconciliation_progress(op, progress=progress)
            op.attempt_token, op.claimed_until = None, None
            if dist.status in {"verifying", "result_unknown"}:
                _source_state(db, context, material, op, "verifying")
                queue_distribution(
                    db,
                    dist,
                    op,
                    kind="verify",
                    due=datetime.now(UTC) + timedelta(seconds=60),
                )
    except Exception as error:
        from app.integrations.tiktok.sdk import AccountAdmissionDeferred

        with Session(database_engine) as db, db.begin():
            material, dist, op = _records(db, context, distribution_id)
            if op.attempt_token != claim:
                return
            not_sent = (
                isinstance(error, AccountAdmissionDeferred)
                or isinstance(error, RemoteCallError)
                and error.effect == "NOT_SENT"
            )
            code = (
                error.code
                if isinstance(error, DomainError)
                else "material_response_unknown"
            )
            # 本次没有调用创建/共享或 adapter 明确 NOT_SENT 才能撤销发送许可。
            unsent = writing and (not sent or not_sent) and not receipt
            if unsent:
                deferred = (
                    isinstance(error, AccountAdmissionDeferred)
                    or code == "admission_unavailable"
                )
                op.status, dist.status = (
                    ("pending", "queued") if deferred else ("failed", "blocked")
                )
                op.remote_response = {**op.remote_response, "send_armed": False}
                if not deferred:
                    _close_unsent(db, context, material, op, code)
            else:
                op.status, dist.status = "result_unknown", "result_unknown"
                _source_state(db, context, material, op, "result_unknown", code)
            op.remote_response = {**op.remote_response, "error_code": code}
            dist.reason_code = code
            op.attempt_token, op.claimed_until = None, None
            if dist.status != "blocked":
                queue_distribution(
                    db,
                    dist,
                    op,
                    kind="prepare" if op.status == "pending" else "verify",
                    due=datetime.now(UTC) + timedelta(seconds=60),
                )


def image_readiness(
    db: Session,
    *,
    context: TenantContext,
    route: FrozenTikTokRoute,
    material: MaterialFile,
    advertiser_id: str,
):
    from .repository import asset_public
    from .schemas import MaterialReadiness

    try:
        _authorize(db, context, route, advertiser_id)
        mapping = _mapping(db, route, material.id, advertiser_id)
        if image_ready(mapping, route):
            return MaterialReadiness(
                state="ready", path="existing_target", mapping=asset_public(mapping)
            )
        pending = db.exec(
            select(MaterialAssetOperation).where(
                MaterialAssetOperation.tenant_id == route.tenant_id,
                MaterialAssetOperation.bc_id == route.bc_id,
                MaterialAssetOperation.material_id == material.id,
                MaterialAssetOperation.advertiser_id == advertiser_id,
                col(MaterialAssetOperation.superseded_by_id).is_(None),
            )
        ).first()
        if pending:
            require_same_route(
                load_material_route(
                    pending.frozen_route, context=context, bc_id=route.bc_id
                ),
                route,
            )
            return MaterialReadiness(state="preparable", path="existing_target")
        sources = db.exec(
            select(AccountMaterial).where(
                AccountMaterial.tenant_id == route.tenant_id,
                AccountMaterial.bc_id == route.bc_id,
                AccountMaterial.material_id == material.id,
                AccountMaterial.media_type == "IMAGE",
                col(AccountMaterial.is_carousel_usable).is_(True),
                AccountMaterial.status == "available",
            )
        ).all()
        for source in sources:
            if (
                image_ready(source, route)
                and source.mid
                and source.connection_id == route.connection_id
            ):
                try:
                    _authorize(
                        db, context, route, source.advertiser_id, capability="read"
                    )
                except DomainError:
                    # 单个失效来源不能遮蔽后续合法副本或同 BC 可用原件。
                    continue
                return MaterialReadiness(state="preparable", path="share_source")
        if material.bc_id == route.bc_id and material.original_available:
            return MaterialReadiness(state="preparable", path="upload_original")
        raise DomainError("image_source_unavailable", "当前 BC 尚无可分发的图片来源")
    except DomainError as error:
        return MaterialReadiness(
            state="blocked",
            path="unavailable",
            reason_code=error.code,
            reason_message=error.message,
        )


def request_image_retry(
    db: Session, *, context: TenantContext, material_id: UUID
) -> UUID:
    from .source_selection import source_file

    require_tenant(
        db, tenant_id=context.tenant_id, actor_id=context.actor_id, action="upload"
    )
    material = _locked_material(db, context, material_id)
    if material.media_type != "IMAGE" or material.current_object is None:
        raise DomainError("image_source_unavailable", "图片原件不可用")
    row = source_file(
        db, context=context, bc_id=material.bc_id, material_id=material.id
    )
    # 既有 UNKNOWN 仅重开只读核查；确定未发送才恢复同一操作的发送许可。
    from .distribution import queue_distribution

    if row.dispatch_id:
        message = db.get(PendingDispatch, row.dispatch_id)
        if message and message.payload.get("distribution_id"):
            _, dist, op = _records(
                db, context, UUID(message.payload["distribution_id"])
            )
            route = _authority(db, context, dist, op)
            if op.attempt_token is not None:
                return row.dispatch_id
            if op.status == "failed" and not op.remote_response.get("send_armed"):
                # 确定未发送的来源失败已释放占用；同一账户重新领取恰好一次。
                if op.remote_response.get(
                    "source_ingest"
                ) and not op.remote_response.get("source_slot_held"):
                    from .source_selection import claim_source_account

                    claim_source_account(
                        db,
                        context=context,
                        bc_id=op.bc_id,
                        material_id=op.material_id,
                        route=route,
                        advertiser_id=op.advertiser_id,
                        reselect=True,
                    )
                    op.remote_response = {
                        **op.remote_response,
                        "source_slot_held": True,
                    }
                op.status, dist.status = "pending", "queued"
                op.remote_response = {
                    **op.remote_response,
                    "revision": op.remote_response.get("revision", 0) + 1,
                }
                queue_distribution(db, dist, op, kind="prepare")
            elif op.status in {"result_unknown", "verifying"}:
                queue_distribution(
                    db, dist, op, kind="verify", observe=True, read_only=True
                )
            return row.dispatch_id
    return enqueue_image_original(
        db, context=context, material=material, obj=material.current_object, row=row
    )


def retry_unsent_image_target(
    db: Session, *, context: TenantContext, distribution_id: UUID
) -> bool:
    """显式重试确定未发送的目标图片，沿用原身份；未知结果仍只能核查。"""
    from .distribution import queue_distribution

    material, dist, op = _records(db, context, distribution_id)
    _authority(db, context, dist, op, writing=True)
    if material.media_type != "IMAGE" or op.remote_response.get("source_ingest"):
        raise DomainError("material_retry_not_allowed", "不是目标图片准备任务")
    if op.status == "pending" and dist.status == "queued":
        return False
    if (
        dist.superseded_by_id
        or op.superseded_by_id
        or dist.status != "blocked"
        or op.status != "failed"
        or op.attempt_token
        or op.claimed_until
        or op.remote_response.get("send_armed") is not False
        or op.remote_response.get("image_id")
        or op.remote_response.get("candidates")
        or op.remote_response.get("share_acknowledged")
    ):
        raise DomainError("material_retry_not_allowed", "原图片结果尚未证实未发送")
    op.status, dist.status, dist.reason_code = "pending", "queued", None
    op.remote_response = {
        **op.remote_response,
        "revision": op.remote_response.get("revision", 0) + 1,
    }
    queue_distribution(db, dist, op, kind="prepare")
    return True
