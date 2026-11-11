"""每轮只准备一个新系列；重投复用子请求标识和原冻结连接。"""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid5

from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.db import engine
from app.core.errors import DomainError
from app.jobs.celery_app import celery_app
from app.jobs.models import PendingDispatch
from app.jobs.outbox import enqueue_after_commit
from app.jobs.tasks import register_dispatch_task
from app.modules.builds.preview_models import BuildPreview, BuildUnit
from app.modules.campaign_copies.models import CampaignCopyDraft
from app.modules.campaign_copies.service import digest, get_draft

TASK = "campaign_copies.prepare"
register_dispatch_task(TASK, "builds")


def queue(session: Session, row: CampaignCopyDraft, *, delay: int = 0) -> None:
    due = datetime.now(UTC) + timedelta(seconds=delay)
    row.repair_after = due + timedelta(seconds=120)
    row.dispatch_id = enqueue_after_commit(
        session,
        context=TenantContext(
            tenant_id=row.tenant_id, actor_id=row.actor_id, role="operator"
        ),
        task_name=TASK,
        task_key=f"campaign-copy:{row.id}:{row.generation}",
        payload={"draft_id": str(row.id), "generation": row.generation},
    )
    dispatch = session.get(PendingDispatch, row.dispatch_id)
    assert dispatch is not None
    dispatch.available_at = due
    session.add_all([row, dispatch])


def process(
    *, database_engine: Any, tenant_id: UUID, actor_id: UUID, payload: dict[str, Any]
) -> None:
    if (
        set(payload) != {"draft_id", "generation"}
        or type(payload["generation"]) is not int
        or payload["generation"] < 0
    ):
        raise DomainError("dispatch_payload_invalid", "复制准备任务参数无效")
    try:
        identity = UUID(payload["draft_id"])
    except TypeError, ValueError, AttributeError:
        raise DomainError("dispatch_payload_invalid", "复制草稿标识无效") from None
    context = TenantContext(tenant_id=tenant_id, actor_id=actor_id, role="operator")
    with Session(database_engine) as session, session.begin():
        # 先锁原父对象；权限撤销必须把任务落为失败，避免Control永远重新投递。
        row = session.exec(
            select(CampaignCopyDraft)
            .where(
                CampaignCopyDraft.tenant_id == tenant_id,
                CampaignCopyDraft.id == identity,
            )
            .with_for_update()
        ).one_or_none()
        if row is None or row.actor_id != actor_id:
            raise DomainError("copy_not_found", "复制准备任务不存在")
        if row.status != "PREPARING" or row.generation != payload["generation"]:
            return
        try:
            get_draft(session, context=context, draft_id=identity, write=True)
            children = deepcopy(row.children)
            child = next((c for c in children if c["status"] == "PENDING"), None)
            if child is not None:
                from app.modules.builds.copy_source import fingerprint, load_copy_source
                from app.modules.builds.copying import create_copy_preview

                source_row = next(r for r in row.rows if r["row_id"] == child["row_id"])
                source = source_row["source"]
                if source.get("snapshot", {}).get("version") != 2:
                    raise DomainError(
                        "copy_source_contract_obsolete", "请重新读取来源后生成复制预览"
                    )
                current = load_copy_source(
                    session,
                    context=context,
                    bc_id=row.bc_id,
                    advertiser_id=source["advertiser_id"],
                    campaign_id=source["campaign_id"],
                )
                if fingerprint(current) != source_row["source_digest"]:
                    raise DomainError(
                        "copy_source_changed",
                        "来源配置已变化，请重新选择来源生成复制草稿",
                    )
                if datetime.fromisoformat(child["started_at"]) + timedelta(
                    hours=6
                ) < datetime.now(UTC):
                    raise DomainError(
                        "copy_preparation_expired", "复制准备超时，请重新生成预览"
                    )
                configuration = deepcopy(source_row["configuration"])
                configuration.update(
                    execution_connection_id=row.route["connection_id"],
                    execution_route=row.route,
                )
                request_id = uuid5(
                    row.id,
                    f"preview:{row.revision}:{child['row_id']}:{child['advertiser_id']}:{child['ordinal']}",
                )
                try:
                    preview_id = create_copy_preview(
                        session,
                        context=context,
                        bc_id=row.bc_id,
                        source=source,
                        configuration=configuration,
                        advertiser_id=child["advertiser_id"],
                        ordinal=child["ordinal"],
                        request_id=request_id,
                    )
                except DomainError as error:
                    if error.code != "copy_preparing":
                        raise
                    # 场景准备的本地派发必须提交；这里不能回滚嵌套事务后再调度。
                    row.generation += 1
                    queue(session, row, delay=3)
                    return
                saved = session.get(BuildPreview, preview_id)
                if (
                    saved is None
                    or saved.tenant_id != tenant_id
                    or saved.bc_id != row.bc_id
                    or saved.status != "FROZEN"
                    or not saved.content_digest
                ):
                    raise DomainError("copy_preview_invalid", "复制子预览尚未完整冻结")
                blocked = session.exec(
                    select(BuildUnit.id)
                    .where(
                        BuildUnit.tenant_id == tenant_id,
                        BuildUnit.preview_id == preview_id,
                        BuildUnit.readiness == "BLOCKED",
                    )
                    .limit(1)
                ).first()
                if blocked is not None:
                    raise DomainError(
                        "copy_target_blocked",
                        "目标账户的复制预览存在不可投放组合，请修正配置",
                    )
                child.update(
                    preview_id=str(preview_id),
                    status="READY",
                    content_digest=saved.content_digest,
                    group_settings=saved.config.get("copy_group_settings", []),
                )
                row.children = children
            if all(c["status"] == "READY" for c in children):
                row.status = "READY"
                row.preview_digest = digest(
                    {
                        "revision": row.revision,
                        "rows": row.rows,
                        "children": children,
                        "route": row.route,
                    }
                )
                session.add(row)
            else:
                row.generation += 1
                queue(session, row)
        except DomainError as error:
            row.status, row.error_code = "FAILED", error.code
            children = deepcopy(row.children)
            pending = next((c for c in children if c["status"] == "PENDING"), None)
            if pending:
                pending.update(status="FAILED", error_code=error.code)
            row.children = children
            session.add(row)


def repair(*, database_engine: Any, limit: int = 100) -> int:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("repair limit must be between 1 and 100")
    now = datetime.now(UTC)
    with Session(database_engine) as session, session.begin():
        from .source_facts import repair_sources

        source_count = repair_sources(session, limit)
        rows = session.exec(
            select(CampaignCopyDraft)
            .where(
                CampaignCopyDraft.status == "PREPARING",
                CampaignCopyDraft.repair_after <= now,
            )
            .order_by(col(CampaignCopyDraft.repair_after), col(CampaignCopyDraft.id))
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for row in rows:
            dispatch = (
                session.get(PendingDispatch, row.dispatch_id)
                if row.dispatch_id
                else None
            )
            if dispatch is None:
                queue(session, row)
            elif (
                dispatch.tenant_id != row.tenant_id
                or dispatch.actor_id != row.actor_id
                or dispatch.task_name != TASK
                or dispatch.payload
                != {"draft_id": str(row.id), "generation": row.generation}
            ):
                row.status, row.error_code = "FAILED", "dispatch_payload_invalid"
            elif dispatch.published_at is not None:
                dispatch.published_at, dispatch.available_at = None, now
                session.add(dispatch)
            row.repair_after = now + timedelta(seconds=120)
            session.add(row)
        return len(rows) + source_count


@celery_app.task(
    name=TASK,
    time_limit=60,
    soft_time_limit=55,
    acks_late=True,
    reject_on_worker_lost=True,
)  # type: ignore[untyped-decorator]
def prepare_task(*, tenant_id: str, actor_id: str, payload: dict[str, Any]) -> None:
    process(
        database_engine=engine,
        tenant_id=UUID(tenant_id),
        actor_id=UUID(actor_id),
        payload=payload,
    )


@celery_app.task(name="campaign_copies.repair", time_limit=60, soft_time_limit=55)  # type: ignore[untyped-decorator]
def repair_task() -> int:
    return repair(database_engine=engine)
