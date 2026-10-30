"""视频成功事件准备实际账户封面；图片 HTTP 不进入视频发布事务。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import String, and_, case, cast, or_
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.diagnostics import emit
from app.core.errors import DomainError
from app.jobs.models import PendingDispatch
from app.jobs.outbox import enqueue_after_commit
from app.jobs.tasks import register_dispatch_task

from .models import AccountMaterial, MaterialAssetOperation, MaterialFile
from .routes import load_material_route

TASK = "materials.prepare_source_cover"
register_dispatch_task(TASK, "control")


def enqueue_source_cover(
    session: Session,
    *,
    context: TenantContext,
    operation: MaterialAssetOperation,
    purpose: str = "SOURCE",
) -> UUID:
    if (
        operation.tenant_id != context.tenant_id
        or purpose not in {"SOURCE", "BUILD"}
        or (purpose == "SOURCE" and operation.path != "upload_original")
        or operation.superseded_by_id is not None
        or operation.status != "succeeded"
    ):
        raise DomainError("source_cover_not_ready", "原视频未完成，不能准备封面")
    identity = enqueue_after_commit(
        session,
        context=context,
        task_name=TASK,
        task_key=f"{'source' if purpose == 'SOURCE' else 'target'}-cover:{operation.id}",
        payload={
            "operation_id": str(operation.id),
            **({"purpose": purpose} if purpose == "BUILD" else {}),
        },
    )
    if purpose == "BUILD":
        # 只补派已登记的目标事件，不让历史所有共享操作每分钟探测 outbox。
        operation.remote_response = {
            **operation.remote_response,
            "target_cover_start_id": str(identity),
        }
    return identity


def prepare_source_cover(
    session: Session,
    *,
    context: TenantContext,
    operation_id: UUID,
    dispatch_id: UUID,
    purpose: str = "SOURCE",
) -> None:
    from .covers import ensure_cover, ensure_source_cover

    prefix = "source" if purpose == "SOURCE" else "target"
    acknowledgment = f"{prefix}_cover_dispatch_id"
    expected_payload = {
        "operation_id": str(operation_id),
        **({"purpose": purpose} if purpose == "BUILD" else {}),
    }
    operation = session.get(MaterialAssetOperation, operation_id)
    dispatch = session.get(PendingDispatch, dispatch_id)
    if (
        operation is None
        or dispatch is None
        or operation.tenant_id != context.tenant_id
        or purpose not in {"SOURCE", "BUILD"}
        or (purpose == "SOURCE" and operation.path != "upload_original")
        or operation.status != "succeeded"
        or dispatch.tenant_id != context.tenant_id
        or dispatch.actor_id != context.actor_id
        or dispatch.task_name != TASK
        or dispatch.task_key != f"{prefix}-cover:{operation_id}"
        or dispatch.payload != expected_payload
    ):
        raise DomainError("invalid_asset_task", "源封面任务与实际上传记录不一致")
    if operation.remote_response.get(acknowledgment) == str(dispatch.id):
        return
    if operation.superseded_by_id is not None:
        _acknowledge(session, operation, dispatch.id, acknowledgment)
        return
    route = load_material_route(
        operation.frozen_route,
        context=context,
        bc_id=operation.bc_id,
    )
    # 事件可能晚于重传完成；内容按租户读取，实际位置由 operation 和 asset 核实。
    # 按封面状态机相同的锁序读取，不能用旧事件启动新 VID。
    material = session.exec(
        select(MaterialFile)
        .where(
            MaterialFile.id == operation.material_id,
            MaterialFile.tenant_id == context.tenant_id,
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
        _acknowledge(session, operation, dispatch.id, acknowledgment)
        return
    # 目标账户沿用 BUILD 共享路径；不会把目标推送改成逐户上传源封面。
    prepare = ensure_source_cover if purpose == "SOURCE" else ensure_cover
    result = prepare(
        session,
        context=context,
        bc_id=operation.bc_id,
        material_id=operation.material_id,
        advertiser_id=operation.advertiser_id,
        task_key=dispatch.task_key,
        route=route,
    )
    emit(
        "cover_start_prepared",
        tenant_id=context.tenant_id,
        bc_id=operation.bc_id,
        operation_id=operation.id,
        dispatch_id=dispatch.id,
        purpose=purpose,
        cover_job_id=result.task_id,
        state=result.state,
        reason_code=result.reason_code,
    )
    if result.reason_code == "cover_video_not_ready" and result.task_id is None:
        # 相同 VID 暂时重核实，没有创建封面任务；保留启动事件供原补派器接续。
        return
    _acknowledge(session, operation, dispatch.id, acknowledgment)


def _acknowledge(
    session: Session,
    operation: MaterialAssetOperation,
    dispatch_id: UUID,
    acknowledgment: str,
) -> None:
    # 与封面身份创建/复用同事务提交；标记的是事件已处理，不冒充远端图片成功。
    session.refresh(operation, with_for_update=True)
    operation.remote_response = {
        **operation.remote_response,
        acknowledgment: str(dispatch_id),
    }
    session.flush()


def repair_source_cover_starts(session: Session, *, limit: int = 100) -> int:
    """只重投未处理的过期事件；已接收任务由封面自身状态机恢复。"""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("source cover repair limit must be between 1 and 100")
    # 两种成功事件共用消费者/修复器；各自确认，不互相覆盖源准备结果。
    target_event = col(PendingDispatch.payload)["purpose"].as_string() == "BUILD"
    acknowledged = case(
        (
            target_event,
            col(MaterialAssetOperation.remote_response)[
                "target_cover_dispatch_id"
            ].as_string(),
        ),
        else_=col(MaterialAssetOperation.remote_response)[
            "source_cover_dispatch_id"
        ].as_string(),
    )
    requested = col(MaterialAssetOperation.remote_response)[
        "target_cover_start_id"
    ].as_string()
    target_ack = col(MaterialAssetOperation.remote_response)[
        "target_cover_dispatch_id"
    ].as_string()
    operation_id = cast(col(MaterialAssetOperation.id), String)
    # 从体量很小的上传记录出发，按 enqueue_source_cover 固定的 task_key 走
    # (tenant_id, task_key) 唯一索引找事件。若按 payload 关联，就得顺序扫描
    # 数百万行的全局 outbox：每分钟一次、每次约 20 秒，会堵住 control 队列的投递。
    rows = session.exec(
        select(PendingDispatch)
        .select_from(MaterialAssetOperation)
        .join(
            PendingDispatch,
            (col(PendingDispatch.tenant_id) == col(MaterialAssetOperation.tenant_id))
            & col(PendingDispatch.task_key).in_(
                ("source-cover:" + operation_id, "target-cover:" + operation_id)
            ),
        )
        .where(
            col(PendingDispatch.payload)["operation_id"].as_string() == operation_id,
            PendingDispatch.task_name == TASK,
            col(PendingDispatch.published_at)
            <= datetime.now(UTC) - timedelta(seconds=60),
            MaterialAssetOperation.status == "succeeded",
            # 先筛操作再查事件唯一键，已完成/未登记目标不能产生无效索引探测。
            or_(
                col(MaterialAssetOperation.path) == "upload_original",
                and_(requested.is_not(None), target_ack.is_distinct_from(requested)),
            ),
            or_(
                and_(
                    target_event,
                    requested == cast(col(PendingDispatch.id), String),
                    target_ack.is_distinct_from(requested),
                    col(PendingDispatch.task_key) == "target-cover:" + operation_id,
                ),
                and_(
                    col(PendingDispatch.payload)["purpose"].as_string().is_(None),
                    col(MaterialAssetOperation.path) == "upload_original",
                    col(PendingDispatch.task_key) == "source-cover:" + operation_id,
                ),
            ),
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
