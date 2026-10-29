"""管理员精确授权后接替一个未知图片共享；不授权来源上传或后续代数。"""

from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import uuid4

from sqlmodel import select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.modules.builds.execution_models import ExecutionStep, Submission
from app.modules.builds.execution_state import evidence

from .distribution import queue_distribution
from .image_distribution import _authority, _mapping, _records
from .models import MaterialAssetOperation, MaterialDistribution


def create_image_replacement(
    session, *, tenant_id, actor_id, submission_id, distribution_id
):
    context = TenantContext(tenant_id=tenant_id, actor_id=actor_id, role="operator")
    material, old, operation = _records(session, context, distribution_id)
    submission = session.get(Submission, submission_id, with_for_update=True)
    steps = session.exec(
        select(ExecutionStep)
        .where(
            ExecutionStep.tenant_id == tenant_id,
            ExecutionStep.submission_id == submission_id,
            ExecutionStep.distribution_id == old.id,
            ExecutionStep.kind == "MATERIAL",
        )
        .with_for_update()
    ).all()
    if (
        not submission
        or submission.tenant_id != tenant_id
        or submission.bc_id != old.bc_id
        or not steps
        or old.superseded_by_id
        or operation.superseded_by_id
        or old.status != "result_unknown"
        or operation.status != "result_unknown"
        or operation.path != "share_source"
        or old.path != operation.path
        or operation.attempt_token
        or operation.claimed_until
        or operation.remote_response.get("image_id")
        or operation.remote_response.get("candidates")
        or operation.remote_response.get("source_ingest")
    ):
        raise DomainError(
            "material_reissue_not_allowed", "原图片操作、提交依赖或冻结来源已改变"
        )
    route = _authority(session, context, old, operation, writing=True)
    if _mapping(session, route, material.id, old.advertiser_id):
        raise DomainError(
            "material_reissue_not_allowed", "目标已有图片身份，必须继续核实"
        )
    # 每个依赖必须来自同一冻结路线；不能拿另一批次授权重建该图片。
    from app.modules.builds.preview_models import BuildUnit
    from app.modules.builds.routes import verify_unit_route

    for step in steps:
        unit = session.get(BuildUnit, step.unit_id)
        if (
            unit is None
            or unit.tenant_id != tenant_id
            or unit.bc_id != old.bc_id
            or unit.advertiser_id != old.advertiser_id
            or unit.preview_id != submission.preview_id
            or step.material_id != old.material_id
            or step.status == "SUCCEEDED"
            or step.remote_id
            or step.dispatch_id
            or step.lease_token
        ):
            raise DomainError("material_reissue_not_allowed", "图片提交依赖已改变")
        unit_route = verify_unit_route(
            session, context=context, unit=unit, capability="build"
        )
        if unit_route.model_dump(mode="json") != old.target_route:
            raise DomainError("material_reissue_not_allowed", "图片冻结连接不一致")
    op_id, dist_id = uuid4(), uuid4()
    operation.superseded_by_id, old.superseded_by_id = op_id, dist_id
    session.flush()
    response = {
        key: operation.remote_response[key]
        for key in (
            "media_type",
            "remote_name",
            "source_image_id",
            "source_mid",
            "source_advertiser_id",
        )
        if key in operation.remote_response
    }
    response.update(revision=0, send_armed=False)
    replacement = MaterialAssetOperation(
        id=op_id,
        tenant_id=tenant_id,
        bc_id=old.bc_id,
        material_id=material.id,
        advertiser_id=old.advertiser_id,
        path=operation.path,
        frozen_route=operation.frozen_route,
        request_digest=sha256(
            f"{operation.request_digest}:{op_id}".encode()
        ).hexdigest(),
        remote_response=response,
    )
    values: dict[str, Any] = {
        **old.model_dump(),
        "id": dist_id,
        "operation_id": op_id,
        "superseded_by_id": None,
        "status": "queued",
        "reason_code": None,
    }
    new = MaterialDistribution(**values)
    session.add(replacement)
    session.flush()
    session.add(new)
    session.flush()
    result = {
        "old_distribution_id": str(old.id),
        "new_distribution_id": str(new.id),
        "old_operation_id": str(operation.id),
        "new_operation_id": str(replacement.id),
        "rebound_step_ids": [str(step.id) for step in steps],
    }
    for step in steps:
        step.distribution_id, step.updated_at = new.id, datetime.now(UTC)
        evidence(
            session,
            step=step,
            claim=None,
            conclusion="MATERIAL_REISSUE_AUTHORIZED",
            summary=result,
        )
    queue_distribution(session, new, replacement, kind="prepare")
    return result
