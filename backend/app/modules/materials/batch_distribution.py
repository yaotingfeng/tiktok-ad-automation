"""从已授权的操作矩阵冻结原生共享批次，保留逐项恢复身份。"""

import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session as SASession
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.bounded_resources import bounded_session
from app.integrations.tiktok.contracts import materials as types
from app.integrations.tiktok.contracts.common import RemoteCallError
from app.integrations.tiktok.gateway import open_tiktok_gateway
from app.integrations.tiktok.sdk import SDK_SCOPE_INTERRUPTS
from app.jobs.admission import admission_policy

from .batch_models import (
    MaterialShareBatch,
    MaterialShareBatchMember,
    MaterialShareBatchReceipt,
)
from .batch_validation import BatchSourceVerifier
from .models import MaterialAssetOperation, MaterialDistribution, MaterialFile
from .routes import load_material_route
from .source_uploads import (
    READ_HARD_LIMIT,
    UPLOAD_HARD_LIMIT,
    _locked_material,
    _locked_operation,
)

SOURCE_KEYS = (
    "source_bc_id",
    "source_material_id",
    "source_asset_id",
    "source_advertiser_id",
    "source_connection_id",
    "source_video_id",
    "content_md5",
)
BATCH_CLAIM_SECONDS = READ_HARD_LIMIT + 30


def rectangle(
    pairs: set[tuple[str, str]], anchor: tuple[str, str]
) -> set[tuple[str, str]]:
    """只选已有完整笛卡尔积；稀疏任务不扩展授权，满矩阵优先装满 20×10。"""
    if anchor not in pairs:
        return set()
    sources = [anchor[0]] + sorted(
        {source for source, target in pairs if target == anchor[1]} - {anchor[0]}
    )
    targets = [anchor[1]] + sorted(
        {target for source, target in pairs if source == anchor[0]} - {anchor[1]}
    )
    selected_targets = targets[:10]
    selected_sources = [
        source
        for source in sources
        if all((source, target) in pairs for target in selected_targets)
    ][:20]
    # 稀疏矩阵选择面积更大的另一种方向，避免为凑目标数牺牲大量可共享素材。
    alternative_sources = sources[:20]
    alternative_targets = [
        target
        for target in targets
        if all((source, target) in pairs for source in alternative_sources)
    ][:10]
    if len(alternative_sources) * len(alternative_targets) > len(
        selected_sources
    ) * len(selected_targets):
        selected_sources, selected_targets = alternative_sources, alternative_targets
    return {
        (source, target) for source in selected_sources for target in selected_targets
    }


def _identity(
    dist: MaterialDistribution, op: MaterialAssetOperation
) -> tuple[Any, ...]:
    return (
        dist.tenant_id,
        dist.bc_id,
        dist.actor_id,
        json.dumps(dist.target_route, sort_keys=True),
        json.dumps(dist.source_route, sort_keys=True),
        op.remote_response.get("source_advertiser_id"),
    )


def try_prepare_batch(
    *,
    database_engine: Any,
    redis_client: Any,
    context: TenantContext,
    distribution_id: UUID,
    operation_id: UUID | None,
    revision: int | None,
    recovery_claim_id: UUID | None,
) -> bool:
    from . import distribution as single

    now = datetime.now(UTC)
    if recovery_claim_id is not None:
        with Session(database_engine) as db:
            recovering = single._load_distribution(db, context, distribution_id)
            original = db.get(MaterialAssetOperation, recovering.operation_id)
            prior_id = (
                original.remote_response.get("share_batch_id") if original else None
            )
            can_recover = (
                original is not None
                and original.status in {"pending", "sending"}
                and prior_id
                and original.attempt_token == recovery_claim_id
                and (operation_id is None or original.id == operation_id)
            )
            armed_recovery = original is not None and original.status == "sending"
        if can_recover and _finish(
            database_engine,
            context,
            UUID(prior_id),
            effect="UNKNOWN" if armed_recovery else "NOT_SENT",
            code="material_armed_worker_expired"
            if armed_recovery
            else "material_prearm_worker_expired",
            recover_unarmed=not armed_recovery,
            recover_armed=armed_recovery,
        ):
            return True
    with Session(database_engine) as db, db.begin():
        anchor = single._load_distribution(db, context, distribution_id)
        anchor_op = db.get(MaterialAssetOperation, anchor.operation_id)
        if (
            anchor_op is None
            or anchor.status != "queued"
            or anchor_op.status != "pending"
        ):
            return False
        if operation_id is not None and anchor_op.id != operation_id:
            return False
        if (
            revision is not None
            and anchor_op.remote_response.get("revision", 0) != revision
        ):
            return False
        if (
            recovery_claim_id is not None
            and anchor_op.attempt_token != recovery_claim_id
        ):
            return False
        if anchor_op.remote_response.get("transport") != "native_share":
            return False
        identity = _identity(anchor, anchor_op)
        # 只串行化同一冻结范围的短领取事务；事务提交后各批远端请求可独立并发。
        # 这样第二个 worker 在第一批领取后重新选矩形，不会吃掉尚未领取的旧消息。
        lock_key = int.from_bytes(
            sha256(repr(identity).encode()).digest()[:8], "big", signed=True
        )
        cast(SASession, db).execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key}
        )
        db.refresh(anchor)
        db.refresh(anchor_op)
        if (
            anchor.status != "queued"
            or anchor_op.status != "pending"
            or (anchor_op.claimed_until and anchor_op.claimed_until > datetime.now(UTC))
        ):
            return True
        if _identity(anchor, anchor_op) != identity:
            return False
        rows = db.exec(
            select(MaterialDistribution, MaterialAssetOperation)
            .join(
                MaterialAssetOperation,
                col(MaterialDistribution.operation_id)
                == col(MaterialAssetOperation.id),
            )
            .where(
                MaterialDistribution.tenant_id == context.tenant_id,
                MaterialDistribution.bc_id == anchor.bc_id,
                MaterialDistribution.actor_id == context.actor_id,
                MaterialDistribution.status == "queued",
                MaterialAssetOperation.status == "pending",
                MaterialAssetOperation.path == "share_source",
            )
            .order_by(
                col(MaterialDistribution.material_id),
                col(MaterialDistribution.advertiser_id),
            )
            .limit(10000)
        ).all()
        candidates = {
            (str(op.remote_response["source_video_id"]), dist.advertiser_id): (dist, op)
            for dist, op in rows
            if _identity(dist, op) == identity
            and op.remote_response.get("transport") == "native_share"
            and isinstance(op.remote_response.get("source_video_id"), str)
            and not op.remote_response.get("send_armed")
            and (op.claimed_until is None or op.claimed_until <= now)
        }
        anchor_pair = (
            str(anchor_op.remote_response.get("source_video_id")),
            anchor.advertiser_id,
        )
        if anchor_pair not in candidates:
            return False
        chosen = rectangle(set(candidates), anchor_pair)
        if len(chosen) < 2:
            return False
        selected = [candidates[pair] for pair in sorted(chosen)]
        # 与旧任务相同，先按素材加锁再锁操作；重复投递只能有一个事务领取。
        for material_id in sorted({dist.material_id for dist, _ in selected}):
            _locked_material(db, context, material_id)
        for dist, op in sorted(selected, key=lambda row: row[1].id):
            db.refresh(dist)
            current = _locked_operation(db, context, op.id)
            if (
                dist.operation_id != current.id
                or dist.status != "queued"
                or current.status != "pending"
                or current.remote_response.get("send_armed")
                or (current.claimed_until and current.claimed_until > now)
                or _identity(dist, current) != identity
            ):
                return False
        claim = uuid4()
        frozen = [
            (str(op.id), op.request_digest, op.remote_response.get("revision", 0))
            for _, op in selected
        ]
        batch = MaterialShareBatch(
            tenant_id=context.tenant_id,
            bc_id=anchor.bc_id,
            actor_id=context.actor_id,
            source_advertiser_id=identity[-1],
            target_route=cast(dict[str, Any], anchor.target_route),
            source_route=cast(dict[str, Any], anchor.source_route),
            claim_id=claim,
            request_digest=sha256(
                json.dumps(frozen, sort_keys=True).encode()
            ).hexdigest(),
            advertiser_ids=sorted({dist.advertiser_id for dist, _ in selected}),
        )
        db.add(batch)
        db.flush()
        for dist, op in selected:
            previous_batch = op.remote_response.get("share_batch_id")
            if previous_batch:
                prior = db.get(MaterialShareBatch, UUID(previous_batch))
                receipt = db.exec(
                    select(MaterialShareBatchReceipt).where(
                        MaterialShareBatchReceipt.batch_id == UUID(previous_batch),
                        MaterialShareBatchReceipt.tenant_id == context.tenant_id,
                        MaterialShareBatchReceipt.effect == "NOT_SENT",
                    )
                ).first()
                if prior is None or prior.status != "not_sent" or receipt is None:
                    raise DomainError(
                        "material_batch_replay_forbidden", "原批次没有完整未发送证据"
                    )
            op.attempt_token = uuid4()
            op.claimed_until = now + timedelta(seconds=BATCH_CLAIM_SECONDS)
            op.remote_response = {
                **op.remote_response,
                "revision": op.remote_response.get("revision", 0) + 1,
                "share_batch_id": str(batch.id),
            }
            db.add(
                MaterialShareBatchMember(
                    tenant_id=context.tenant_id,
                    bc_id=dist.bc_id,
                    batch_id=batch.id,
                    material_id=dist.material_id,
                    advertiser_id=dist.advertiser_id,
                    distribution_id=dist.id,
                    operation_id=op.id,
                    operation_claim=op.attempt_token,
                    operation_digest=op.request_digest,
                    source_video_id=op.remote_response["source_video_id"],
                    source_evidence={
                        key: op.remote_response.get(key) for key in SOURCE_KEYS
                    },
                    revision=op.remote_response["revision"],
                )
            )
            single.queue_distribution(
                db,
                dist,
                op,
                kind="prepare",
                due=op.claimed_until,
                claim_id=op.attempt_token,
            )
        batch_id = batch.id
    _send_batch(
        database_engine=database_engine,
        redis_client=redis_client,
        context=context,
        batch_id=batch_id,
    )
    return True


def _members(
    db: Session, context: TenantContext, batch_id: UUID
) -> list[MaterialShareBatchMember]:
    return list(
        db.exec(
            select(MaterialShareBatchMember)
            .where(
                MaterialShareBatchMember.tenant_id == context.tenant_id,
                MaterialShareBatchMember.batch_id == batch_id,
            )
            .order_by(
                col(MaterialShareBatchMember.material_id),
                col(MaterialShareBatchMember.operation_id),
            )
        ).all()
    )


def _locked_batch_rows(
    db: Session, context: TenantContext, batch_id: UUID
) -> list[
    tuple[
        MaterialShareBatchMember,
        MaterialDistribution,
        MaterialFile,
        MaterialAssetOperation,
    ]
]:
    """固定四个批量查询，始终先锁全部素材再锁全部操作，避免 N×M 往返与锁反序。"""
    members = _members(db, context, batch_id)
    materials = {
        row.id: row
        for row in db.exec(
            select(MaterialFile)
            .where(
                MaterialFile.tenant_id == context.tenant_id,
                col(MaterialFile.id).in_({member.material_id for member in members}),
            )
            .order_by(col(MaterialFile.id))
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    }
    operations = {
        row.id: row
        for row in db.exec(
            select(MaterialAssetOperation)
            .where(
                MaterialAssetOperation.tenant_id == context.tenant_id,
                col(MaterialAssetOperation.id).in_(
                    {member.operation_id for member in members}
                ),
            )
            .order_by(col(MaterialAssetOperation.id))
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    }
    distributions = {
        row.id: row
        for row in db.exec(
            select(MaterialDistribution)
            .where(
                MaterialDistribution.tenant_id == context.tenant_id,
                col(MaterialDistribution.id).in_(
                    {member.distribution_id for member in members}
                ),
            )
            .execution_options(populate_existing=True)
        ).all()
    }
    result = []
    for member in members:
        dist, material, op = (
            distributions.get(member.distribution_id),
            materials.get(member.material_id),
            operations.get(member.operation_id),
        )
        if (
            dist is None
            or material is None
            or op is None
            or dist.actor_id != context.actor_id
            or dist.bc_id != member.bc_id
            or dist.material_id != member.material_id
            or dist.advertiser_id != member.advertiser_id
        ):
            raise DomainError("material_claim_changed", "共享批次成员范围已改变")
        result.append((member, dist, material, op))
    return result


def _send_batch(
    *, database_engine: Any, redis_client: Any, context: TenantContext, batch_id: UUID
) -> None:
    from . import distribution as single
    from . import sdk_assets as api

    # 单次共享没有视频上传字节，但完整矩阵的本地鉴权也必须有明确硬期限。
    deadline = datetime.now(UTC) + timedelta(seconds=READ_HARD_LIMIT - 5)
    with Session(database_engine) as db:
        batch = db.get(MaterialShareBatch, batch_id)
        assert batch is not None
        source_ids = tuple(
            sorted(
                {member.source_video_id for member in _members(db, context, batch_id)}
            )
        )
        route = load_material_route(
            batch.target_route, context=context, bc_id=batch.bc_id
        )
        source = batch.source_advertiser_id
        targets = tuple(batch.advertiser_ids)
    armed = False
    acknowledged = False
    receipt: types.AssetShareReceipt | None = None
    missing_sources: set[str] = set()

    def check_current() -> None:
        with bounded_session(database_engine, task_deadline=deadline) as db, db.begin():
            current = db.get(MaterialShareBatch, batch_id)
            if current is None or current.status not in {"pending", "sending"}:
                raise DomainError("material_claim_changed", "共享批次已结束")
            seen_sources: set[UUID] = set()
            seen_targets: set[str] = set()
            rows = _locked_batch_rows(db, context, batch_id)
            source_route = load_material_route(
                current.source_route, context=context, bc_id=current.bc_id
            )
            verifier = BatchSourceVerifier(
                db,
                context=context,
                source_bc_id=source_route.bc_id,
                source_asset_ids={
                    UUID(row[0].source_evidence["source_asset_id"]) for row in rows
                },
                materials=(row[2] for row in rows),
            )
            for member, dist, material, op in rows:
                if (
                    dist.operation_id != op.id
                    or op.attempt_token != member.operation_claim
                    or op.claimed_until is None
                    or op.request_digest != member.operation_digest
                    or op.remote_response.get("revision") != member.revision
                    or op.frozen_route != current.target_route
                    or dist.target_route != current.target_route
                    or dist.source_route != current.source_route
                    or op.remote_response.get("source_video_id")
                    != member.source_video_id
                    or {key: op.remote_response.get(key) for key in SOURCE_KEYS}
                    != member.source_evidence
                ):
                    raise DomainError("material_claim_changed", "共享成员身份已改变")
                if op.claimed_until <= datetime.now(UTC):
                    raise DomainError("material_claim_expired", "共享成员领取已过期")
                if member.material_id not in seen_sources:
                    try:
                        single._require_distribution_source(
                            db,
                            context=context,
                            material=material,
                            operation=op,
                            source_route=source_route,
                            verifier=verifier,
                        )
                    except DomainError as error:
                        if (
                            error.code
                            in {
                                "material_remote_source_unavailable",
                                "material_source_digest_changed",
                            }
                            and not armed
                        ):
                            missing_sources.add(member.source_video_id)
                        raise
                    seen_sources.add(member.material_id)
                elif op.remote_response.get("content_md5") != material.video_md5:
                    raise DomainError(
                        "material_source_digest_changed", "共享成员摘要已改变"
                    )
                if dist.advertiser_id not in seen_targets:
                    for capability in ("build", "upload"):
                        verifier.require_route(
                            db,
                            context=context,
                            route=route,
                            bc_id=dist.bc_id,
                            advertiser_id=dist.advertiser_id,
                            capability=capability,
                        )
                    seen_targets.add(dist.advertiser_id)
            verifier.recheck_transaction()

    try:
        check_current()
        with open_tiktok_gateway(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            route=route,
            task_deadline=deadline,
            before_request=check_current,
        ) as gateway:
            records = gateway.materials.read_videos(
                advertiser_id=source,
                video_ids=source_ids,
                budget=types.RemoteCallBudget(
                    deadline,
                    READ_HARD_LIMIT,
                    admission_policy("materials.get_videos").lease_ms,
                ),
            )
            by_id = {record.video_id: record for record in records}
            with (
                bounded_session(database_engine, task_deadline=deadline) as db,
                db.begin(),
            ):
                current = db.get(MaterialShareBatch, batch_id)
                assert current is not None
                mids = set()
                for member, _dist, _material, op in _locked_batch_rows(
                    db, context, batch_id
                ):
                    record = by_id.get(member.source_video_id)
                    if (
                        record is None
                        or not record.mid
                        or not record.file_name
                        or record.md5 != op.remote_response.get("content_md5")
                    ):
                        missing_sources.add(member.source_video_id)
                        continue
                    if op.attempt_token != member.operation_claim:
                        raise DomainError("material_claim_changed", "共享成员已被接管")
                    member.source_mid = record.mid
                    mids.add(record.mid)
                    op.remote_response = {
                        **op.remote_response,
                        "source_mid": record.mid,
                        "remote_name": record.file_name,
                    }
                if missing_sources:
                    raise DomainError(
                        "material_share_source_missing",
                        "部分批次来源缺少可核实的共享证据",
                    )
                current.material_ids = sorted(mids)
                current.wire_digest = sha256(
                    json.dumps(
                        {
                            "advertiser_id": source,
                            "asset_type": "VIDEO",
                            "material_ids": sorted(mids),
                            "shared_advertiser_ids": list(targets),
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
            check_current()
            with (
                bounded_session(database_engine, task_deadline=deadline) as db,
                db.begin(),
            ):
                current = db.get(MaterialShareBatch, batch_id)
                assert current is not None
                for member, dist, _material, op in _locked_batch_rows(
                    db, context, batch_id
                ):
                    if (
                        op.attempt_token != member.operation_claim
                        or dist.operation_id != op.id
                    ):
                        raise DomainError("material_claim_changed", "共享成员已被接管")
                    op.status, dist.status = "sending", "preparing"
                    op.remote_response = {
                        **op.remote_response,
                        "send_armed": True,
                        "upload_connection_id": str(route.connection_id),
                    }
                current.status = "sending"
            armed = True
            receipt = gateway.materials.share_assets(
                types.AssetShare(
                    advertiser_id=source,
                    material_ids=tuple(sorted(mids)),
                    shared_advertiser_ids=targets,
                ),
                budget=types.RemoteCallBudget(
                    deadline,
                    UPLOAD_HARD_LIMIT,
                    admission_policy("materials.share_assets").lease_ms,
                ),
            )
            acknowledged = True
            _finish(
                database_engine,
                context,
                batch_id,
                effect="ACKNOWLEDGED",
                code=None,
                receipt=receipt,
            )
    except SDK_SCOPE_INTERRUPTS:
        raise
    except Exception as error:
        not_sent = (
            isinstance(error, api.SdkAdmissionDeferred)
            or (isinstance(error, RemoteCallError) and error.effect == "NOT_SENT")
            or (
                isinstance(error, DomainError)
                and error.code == "material_claim_expired"
                and not armed
            )
        )
        effect = (
            "ACKNOWLEDGED"
            if acknowledged
            else "NOT_SENT"
            if not_sent or missing_sources
            else "UNKNOWN"
            if armed
            else "FAILED"
        )
        _finish(
            database_engine,
            context,
            batch_id,
            effect=effect,
            code=error.code
            if isinstance(error, DomainError)
            else "material_response_unknown",
            missing_sources=missing_sources,
            receipt=receipt,
        )


def _finish(
    database_engine: Any,
    context: TenantContext,
    batch_id: UUID,
    *,
    effect: str,
    code: str | None,
    recover_unarmed: bool = False,
    recover_armed: bool = False,
    missing_sources: set[str] | None = None,
    receipt: types.AssetShareReceipt | None = None,
) -> bool:
    from . import distribution as single

    with Session(database_engine) as db, db.begin():
        # 与逐项/批量核验统一为全部素材 -> 全部操作 -> 批次，避免账本收口
        # 持有批次锁等待素材时，与已经持有素材的核验互相等待。
        rows = _locked_batch_rows(db, context, batch_id)
        batch = db.exec(
            select(MaterialShareBatch)
            .where(
                MaterialShareBatch.id == batch_id,
                MaterialShareBatch.tenant_id == context.tenant_id,
            )
            .with_for_update()
        ).first()
        assert batch is not None
        if batch.status not in {"pending", "sending"}:
            return False
        if recover_unarmed or recover_armed:
            if batch.status != ("sending" if recover_armed else "pending"):
                return False
            for member in _members(db, context, batch_id):
                _locked_material(db, context, member.material_id)
                op = _locked_operation(db, context, member.operation_id)
                if (
                    op.status != ("sending" if recover_armed else "pending")
                    or op.attempt_token != member.operation_claim
                    or (bool(op.remote_response.get("send_armed")) != recover_armed)
                    or op.claimed_until is None
                    or op.claimed_until > datetime.now(UTC)
                ):
                    return False
        batch.status = {
            "ACKNOWLEDGED": "acknowledged",
            "UNKNOWN": "result_unknown",
            "NOT_SENT": "not_sent",
            "FAILED": "failed",
        }[effect]
        partial_failure = bool(receipt and any(receipt.failed_infos.values()))
        db.add(
            MaterialShareBatchReceipt(
                tenant_id=batch.tenant_id,
                bc_id=batch.bc_id,
                batch_id=batch.id,
                effect=effect,
                code="material_share_partial_failure" if partial_failure else code,
                share_response=asdict(receipt) if receipt is not None else None,
            )
        )
        for member, dist, _material, op in rows:
            if op.attempt_token != member.operation_claim or dist.operation_id != op.id:
                continue
            rejected = bool(
                receipt
                and member.source_mid
                in receipt.failed_infos.get(member.advertiser_id, ())
            )
            member_code = "material_share_failed" if rejected else code
            member_effect = (
                "FAILED"
                if rejected or member.source_video_id in (missing_sources or set())
                else effect
            )
            member.status = {
                "ACKNOWLEDGED": "verifying",
                "UNKNOWN": "result_unknown",
                "NOT_SENT": "not_sent",
                "FAILED": "failed",
            }[member_effect]
            if receipt is not None:
                # ACKNOWLEDGED 只表示取得合法回执，业务失败必须按目标/MID 分开。
                op.remote_response = {
                    **op.remote_response,
                    "share_acknowledged": not rejected,
                    "share_request_id": receipt.evidence.request_id,
                }
            if member_effect == "ACKNOWLEDGED":
                op.status, dist.status = "verifying", "verifying"
                op.remote_response = {**op.remote_response, "share_acknowledged": True}
            elif member_effect == "NOT_SENT":
                op.status, dist.status = "pending", "queued"
                op.remote_response = {**op.remote_response, "send_armed": False}
            elif member_effect == "FAILED":
                op.status, dist.status = "failed", "blocked"
                op.remote_response = {
                    **op.remote_response,
                    "definite_no_effect": True,
                    "error_code": member_code,
                }
            else:
                op.status, dist.status = "result_unknown", "result_unknown"
                op.remote_response = {**op.remote_response, "error_code": member_code}
            op.attempt_token, op.claimed_until = None, None
            dist.reason_code = member_code or "material_result_pending"
            if member_effect != "FAILED":
                single.queue_distribution(
                    db,
                    dist,
                    op,
                    kind="prepare" if effect == "NOT_SENT" else "verify",
                    due=datetime.now(UTC)
                    + timedelta(seconds=60 if effect in {"NOT_SENT", "UNKNOWN"} else 0),
                )
        return True


def sync_batch_member(
    db: Session, op: MaterialAssetOperation, dist: MaterialDistribution
) -> None:
    """逐项核实沿用原任务；已完成成员不会再进入共享或读取队列。"""
    batch_id = op.remote_response.get("share_batch_id")
    if not batch_id:
        return
    batch = db.exec(
        select(MaterialShareBatch)
        .where(
            MaterialShareBatch.tenant_id == op.tenant_id,
            MaterialShareBatch.id == UUID(batch_id),
        )
        .with_for_update()
    ).first()
    if batch is None:
        return
    member = db.exec(
        select(MaterialShareBatchMember).where(
            MaterialShareBatchMember.tenant_id == op.tenant_id,
            MaterialShareBatchMember.batch_id == UUID(batch_id),
            MaterialShareBatchMember.operation_id == op.id,
        )
    ).first()
    if member is not None:
        member.status = (
            dist.status
            if dist.status in {"ready", "verifying", "result_unknown"}
            else "failed"
        )
        db.flush()
        if batch.status == "sending":
            # 旧版逐项恢复消息也能收口批次；任何已 arm 后丢失的回执只能记 UNKNOWN。
            batch.status = "result_unknown"
            db.add(
                MaterialShareBatchReceipt(
                    tenant_id=batch.tenant_id,
                    bc_id=batch.bc_id,
                    batch_id=batch.id,
                    effect="UNKNOWN",
                    code="material_batch_member_recovered",
                )
            )
        incomplete = db.exec(
            select(MaterialShareBatchMember.id)
            .where(
                MaterialShareBatchMember.batch_id == batch.id,
                MaterialShareBatchMember.status != "ready",
            )
            .limit(1)
        ).first()
        if incomplete is None:
            batch.status = "completed"
