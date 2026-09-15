"""视频成功事件只创建源封面任务；图片 HTTP 不进入视频发布事务。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import String, cast, or_
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.jobs.models import PendingDispatch
from app.jobs.outbox import enqueue_after_commit
from app.jobs.tasks import register_dispatch_task

from .models import AccountMaterial, MaterialAssetOperation, MaterialFile
from .routes import load_material_route

TASK = "materials.prepare_source_cover"
register_dispatch_task(TASK, "control")


def enqueue_source_cover(
    session: Session, *, context: TenantContext, operation: MaterialAssetOperation
) -> UUID:
    if (
        operation.tenant_id != context.tenant_id
        or operation.path != "upload_original"
        or operation.status != "succeeded"
    ):
        raise DomainError("source_cover_not_ready", "原视频未完成，不能准备封面")
    return enqueue_after_commit(
        session,
        context=context,
        task_name=TASK,
        task_key=f"source-cover:{operation.id}",
        payload={"operation_id": str(operation.id)},
    )


def prepare_source_cover(
    session: Session, *, context: TenantContext, operation_id: UUID, dispatch_id: UUID
) -> None:
    from .covers import ensure_source_cover

    operation = session.get(MaterialAssetOperation, operation_id)
    dispatch = session.get(PendingDispatch, dispatch_id)
    if (
        operation is None
        or dispatch is None
        or operation.tenant_id != context.tenant_id
        or operation.path != "upload_original"
        or operation.status != "succeeded"
        or dispatch.tenant_id != context.tenant_id
        or dispatch.actor_id != context.actor_id
        or dispatch.task_name != TASK
        or dispatch.task_key != f"source-cover:{operation_id}"
        or dispatch.payload != {"operation_id": str(operation_id)}
    ):
        raise DomainError("invalid_asset_task", "源封面任务与实际上传记录不一致")
    if operation.remote_response.get("source_cover_dispatch_id") == str(dispatch.id):
        return
    route = load_material_route(
        operation.frozen_route,
        context=context,
        bc_id=operation.bc_id,
    )
    # 事件可能晚于重传完成；按封面状态机相同的锁序读取，不能用旧事件启动新 VID。
    material = session.exec(
        select(MaterialFile)
        .where(
            MaterialFile.id == operation.material_id,
            MaterialFile.tenant_id == context.tenant_id,
            MaterialFile.bc_id == operation.bc_id,
        )
        .with_for_update()
    ).first()
    asset = session.exec(
        select(AccountMaterial)
        .where(
            AccountMaterial.tenant_id == context.tenant_id,
            AccountMaterial.bc_id == operation.bc_id,
            AccountMaterial.material_id == operation.material_id,
            AccountMaterial.advertiser_id == operation.advertiser_id,
        )
        .with_for_update()
    ).first()
    if (
        material is None
        or asset is None
        or asset.video_id != operation.remote_response.get("video_id")
        or asset.connection_id != route.connection_id
    ):
        _acknowledge(session, operation, dispatch.id)
        return
    ensure_source_cover(
        session,
        context=context,
        bc_id=operation.bc_id,
        material_id=operation.material_id,
        advertiser_id=operation.advertiser_id,
        task_key=dispatch.task_key,
        route=route,
    )
    _acknowledge(session, operation, dispatch.id)


def _acknowledge(
    session: Session, operation: MaterialAssetOperation, dispatch_id: UUID
) -> None:
    # 与封面身份创建/复用同事务提交；标记的是事件已处理，不冒充远端图片成功。
    session.refresh(operation, with_for_update=True)
    operation.remote_response = {
        **operation.remote_response,
        "source_cover_dispatch_id": str(dispatch_id),
    }
    session.flush()


def repair_source_cover_starts(session: Session, *, limit: int = 100) -> int:
    """只重投未处理的过期事件；已接收任务由封面自身状态机恢复。"""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("source cover repair limit must be between 1 and 100")
    acknowledged = col(MaterialAssetOperation.remote_response)[
        "source_cover_dispatch_id"
    ].as_string()
    rows = session.exec(
        select(PendingDispatch)
        .join(
            MaterialAssetOperation,
            (MaterialAssetOperation.tenant_id == PendingDispatch.tenant_id)
            & (
                col(PendingDispatch.payload)["operation_id"].as_string()
                == cast(col(MaterialAssetOperation.id), String)
            ),
        )
        .where(
            PendingDispatch.task_name == TASK,
            col(PendingDispatch.published_at)
            <= datetime.now(UTC) - timedelta(seconds=60),
            MaterialAssetOperation.status == "succeeded",
            MaterialAssetOperation.path == "upload_original",
            or_(
                acknowledged.is_(None),
                acknowledged != cast(col(PendingDispatch.id), String),
            ),
        )
        .order_by(col(PendingDispatch.published_at), col(PendingDispatch.id))
        .limit(limit)
        .with_for_update(of=PendingDispatch, skip_locked=True)
    ).all()
    for row in rows:
        row.published_at = None
    session.flush()
    return len(rows)
