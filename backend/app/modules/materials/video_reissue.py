"""精确授权的未知视频补发；只接替当前头，不把未知伪装成未生效。"""

from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, NoReturn
from uuid import UUID, uuid4

from sqlalchemy import func, or_
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.modules.builds.execution_models import ExecutionStep, Submission
from app.modules.builds.execution_state import evidence
from app.modules.builds.preview_models import BuildUnit
from app.modules.builds.routes import verify_unit_route
from app.modules.tenants.permissions import require_tenant

from .content_identity import content_key
from .distribution import _require_distribution_source, queue_distribution
from .file_names import video_file_name
from .models import (
    AccountMaterial,
    MaterialAssetOperation,
    MaterialDistribution,
    MaterialFile,
    MaterialUploadAttempt,
)
from .routes import load_material_route, require_material_route, require_same_route
from .seed_models import MaterialBCSeed


def _reject() -> NoReturn:
    raise DomainError(
        "material_reissue_not_allowed", "原视频操作、批次依赖或冻结来源已改变"
    )


def create_video_replacement(
    session: Session,
    *,
    tenant_id: UUID,
    actor_id: UUID,
    submission_id: UUID,
    distribution_id: UUID,
) -> dict[str, Any]:
    """由外层授权账本事务调用；本方法不提交，也不授予后续代数许可。"""
    context = TenantContext(tenant_id=tenant_id, actor_id=actor_id, role="operator")
    require_tenant(session, actor_id=actor_id, tenant_id=tenant_id, action="build")
    submission = session.exec(
        select(Submission)
        .where(
            Submission.tenant_id == tenant_id,
            Submission.id == submission_id,
        )
        .with_for_update()
    ).one_or_none()
    old = session.get(MaterialDistribution, distribution_id)
    if (
        submission is None
        or old is None
        or (old.tenant_id, old.bc_id) != (tenant_id, submission.bc_id)
    ):
        _reject()
    assert old and submission
    require_tenant(
        session, actor_id=submission.actor_id, tenant_id=tenant_id, action="build"
    )
    old_seed = session.exec(
        select(MaterialBCSeed).where(
            MaterialBCSeed.tenant_id == tenant_id,
            MaterialBCSeed.bc_id == old.bc_id,
            MaterialBCSeed.distribution_id == old.id,
        )
    ).one_or_none()
    # 同批重复请求在外层账本命中；此处仍校验已有新头属于原提交，不能跨批借用。
    lookup_id = old.superseded_by_id or old.id
    lookup_seed = session.exec(
        select(MaterialBCSeed).where(
            MaterialBCSeed.tenant_id == tenant_id,
            MaterialBCSeed.distribution_id == lookup_id,
        )
    ).one_or_none()
    dependencies = (
        select(MaterialDistribution.id).where(
            MaterialDistribution.tenant_id == tenant_id,
            MaterialDistribution.seed_id == lookup_seed.id,
        )
        if lookup_seed
        else select(MaterialDistribution.id).where(MaterialDistribution.id == lookup_id)
    )
    steps = session.exec(
        select(ExecutionStep)
        .where(
            ExecutionStep.tenant_id == tenant_id,
            ExecutionStep.submission_id == submission_id,
            ExecutionStep.kind == "MATERIAL",
            col(ExecutionStep.cover_job_id).is_(None),
            or_(
                col(ExecutionStep.distribution_id) == lookup_id,
                col(ExecutionStep.distribution_id).in_(dependencies),
            ),
        )
        .order_by(col(ExecutionStep.id))
        .with_for_update()
    ).all()
    if not steps:
        _reject()
    if old.superseded_by_id:
        new = session.get(MaterialDistribution, old.superseded_by_id)
        assert new
        return {
            "old_distribution_id": str(old.id),
            "new_distribution_id": str(new.id),
            "old_operation_id": str(old.operation_id),
            "new_operation_id": str(new.operation_id),
            "old_seed_id": str(old_seed.id) if old_seed else None,
            "new_seed_id": str(lookup_seed.id) if lookup_seed else None,
            "rebound_consumer_ids": [],
            "rebound_step_ids": [],
        }
    consumers = session.exec(
        select(MaterialDistribution).where(
            MaterialDistribution.tenant_id == tenant_id,
            col(MaterialDistribution.id).in_({step.distribution_id for step in steps}),
            MaterialDistribution.id != old.id,
        )
    ).all()
    # 与发送器使用相同素材→操作锁序；将所有别名素材按固定顺序一次锁取。
    materials = session.exec(
        select(MaterialFile)
        .where(
            MaterialFile.tenant_id == tenant_id,
            col(MaterialFile.id).in_(
                {old.material_id, *(dist.material_id for dist in consumers)}
            ),
        )
        .order_by(col(MaterialFile.id))
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    material = next(row for row in materials if row.id == old.material_id)
    operation = session.exec(
        select(MaterialAssetOperation)
        .where(
            MaterialAssetOperation.tenant_id == tenant_id,
            MaterialAssetOperation.id == old.operation_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    session.refresh(old)
    if old.superseded_by_id:
        # 跨提交竞争不继承另一提交的接替许可；外层应返回原授权结果。
        _reject()
    positive_keys = {
        "video_id",
        "mid",
        "upload_video_id",
        "verified_upload_video_id",
        "conflicting_video_id",
    }
    if (
        old.status != "result_unknown"
        or operation.status != "result_unknown"
        or old.path != "share_source"
        or operation.path != old.path
        or operation.superseded_by_id is not None
        or operation.attempt_token is not None
        or operation.claimed_until is not None
        or any(operation.remote_response.get(key) for key in positive_keys)
        or operation.remote_response.get("candidates")
        or old.seed_id is not None
        or old.source_route is None
        or old.source_bc_id is None
        or old.target_route != operation.frozen_route
        or str(old.source_asset_id) != operation.remote_response.get("source_asset_id")
        or str(old.source_material_id)
        != operation.remote_response.get("source_material_id")
        or old.source_bc_id != operation.remote_response.get("source_bc_id")
        or operation.remote_response.get("transport")
        not in {"native_share", "url_relay"}
    ):
        _reject()
    if session.exec(
        select(MaterialUploadAttempt.id).where(
            MaterialUploadAttempt.operation_id == operation.id
        )
    ).first():
        _reject()
    if session.exec(
        select(AccountMaterial.id).where(
            AccountMaterial.tenant_id == tenant_id,
            AccountMaterial.bc_id == old.bc_id,
            AccountMaterial.material_id == old.material_id,
            AccountMaterial.advertiser_id == old.advertiser_id,
            func.length(func.trim(AccountMaterial.video_id)) > 0,
        )
    ).first():
        _reject()
    route = load_material_route(old.target_route, context=context, bc_id=old.bc_id)
    require_material_route(
        session,
        context=context,
        route=route,
        bc_id=old.bc_id,
        advertiser_id=old.advertiser_id,
        capability="build",
    )
    require_tenant(session, actor_id=old.actor_id, tenant_id=tenant_id, action="build")
    _require_distribution_source(
        session,
        context=context,
        material=material,
        operation=operation,
        source_route=load_material_route(
            old.source_route, context=context, bc_id=old.source_bc_id
        ),
    )
    for step in steps:
        unit = session.get(BuildUnit, step.unit_id)
        dependency = (
            old
            if step.distribution_id == old.id
            else next(
                (dist for dist in consumers if dist.id == step.distribution_id), None
            )
        )
        if (
            unit is None
            or dependency is None
            or (step.tenant_id, step.bc_id, step.material_id, unit.advertiser_id)
            != (
                dependency.tenant_id,
                dependency.bc_id,
                dependency.material_id,
                dependency.advertiser_id,
            )
            or step.status == "SUCCEEDED"
            or step.remote_id
            or step.dispatch_id
            or step.lease_token
        ):
            _reject()
        require_same_route(
            verify_unit_route(session, context=context, unit=unit, capability="build"),
            route,
        )
    for consumer in consumers:
        # 等待素材锁期间另一 worker 可能已绑定来源，必须重读分发后再检查。
        session.refresh(consumer)
        op = session.exec(
            select(MaterialAssetOperation)
            .where(
                MaterialAssetOperation.id == consumer.operation_id,
                MaterialAssetOperation.tenant_id == tenant_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()
        if (
            old_seed is None
            or consumer.seed_id != old_seed.id
            or consumer.superseded_by_id
            or op.superseded_by_id
            or consumer.target_route != old.target_route
            or consumer.source_asset_id is not None
            or consumer.status not in {"queued", "result_unknown"}
            or op.status != "pending"
            or op.attempt_token
            or op.claimed_until
            or op.remote_response.get("transport")
            or op.remote_response.get("send_armed")
            or content_key(
                next(row for row in materials if row.id == consumer.material_id)
            )
            != old_seed.content_key
        ):
            _reject()
    next_generation = None
    if old_seed:
        if content_key(material) != old_seed.content_key:
            _reject()
        lock = int.from_bytes(
            sha256(
                f"bc-seed:{tenant_id}:{old.bc_id}:{old_seed.content_key}".encode()
            ).digest()[:8],
            "big",
            signed=True,
        )
        session.exec(select(func.pg_advisory_xact_lock(lock))).one()
        latest = session.exec(
            select(MaterialBCSeed)
            .where(
                MaterialBCSeed.tenant_id == tenant_id,
                MaterialBCSeed.bc_id == old.bc_id,
                MaterialBCSeed.content_key == old_seed.content_key,
            )
            .order_by(col(MaterialBCSeed.generation).desc())
        ).first()
        if latest is None or latest.id != old_seed.id:
            _reject()
        next_generation = old_seed.generation + 1
    new_operation_id, new_distribution_id = uuid4(), uuid4()
    old.superseded_by_id, operation.superseded_by_id = (
        new_distribution_id,
        new_operation_id,
    )
    # 延迟自引用 FK 允许先释放当前头唯一索引；旧状态和全部回执保持原样。
    session.flush()
    source_keys = {
        "transport",
        "source_bc_id",
        "source_material_id",
        "source_asset_id",
        "source_advertiser_id",
        "source_connection_id",
        "source_video_id",
        "content_md5",
    }
    response = {
        key: operation.remote_response[key]
        for key in source_keys
        if key in operation.remote_response
    }
    response["remote_name"] = video_file_name(
        material.file_name, correlation=new_operation_id.hex[:8]
    )
    new_operation = MaterialAssetOperation(
        id=new_operation_id,
        tenant_id=tenant_id,
        bc_id=old.bc_id,
        material_id=old.material_id,
        advertiser_id=old.advertiser_id,
        path=operation.path,
        frozen_route=operation.frozen_route,
        request_digest=sha256(
            f"{operation.request_digest}:{new_operation_id}".encode()
        ).hexdigest(),
        remote_response=response,
    )
    new_values: dict[str, Any] = {
        **old.model_dump(),
        "id": new_distribution_id,
        "operation_id": new_operation_id,
        "superseded_by_id": None,
        "status": "queued",
        "reason_code": None,
    }
    new = MaterialDistribution(**new_values)
    session.add(new_operation)
    session.flush()
    session.add(new)
    session.flush()
    new_seed = None
    if old_seed:
        seed_values: dict[str, Any] = {
            **old_seed.model_dump(),
            "id": uuid4(),
            "distribution_id": new.id,
            "generation": next_generation,
        }
        new_seed = MaterialBCSeed(**seed_values)
        session.add(new_seed)
        session.flush()
        for consumer in consumers:
            consumer.seed_id = new_seed.id
            consumer.status, consumer.reason_code = "queued", "material_seed_pending"
    result = {
        "old_distribution_id": str(old.id),
        "new_distribution_id": str(new.id),
        "old_operation_id": str(operation.id),
        "new_operation_id": str(new_operation.id),
        "old_seed_id": str(old_seed.id) if old_seed else None,
        "new_seed_id": str(new_seed.id) if new_seed else None,
        "rebound_consumer_ids": [str(dist.id) for dist in consumers],
        "rebound_step_ids": [str(step.id) for step in steps],
    }
    for step in steps:
        if step.distribution_id == old.id:
            step.distribution_id = new.id
        step.updated_at = datetime.now(UTC)
        evidence(
            session,
            step=step,
            claim=None,
            conclusion="MATERIAL_REISSUE_AUTHORIZED",
            summary=result,
        )
    queue_distribution(session, new, new_operation, kind="prepare")
    session.flush()
    return result
