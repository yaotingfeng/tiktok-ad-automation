"""草稿投放排期随“生成搭建预览”保存，沿用已准备的账户、剧目、链接和素材。"""

from uuid import UUID, uuid5

from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError

from .models import DraftPreparation
from .schemas import DeliveryChange


def save_delivery(
    session: Session, *, context: TenantContext, draft_id: UUID, body: DeliveryChange
) -> int:
    from .drafts import _bump_revision, get_draft
    from .mutations import _apply

    def apply() -> int:
        draft = get_draft(session, context=context, draft_id=draft_id, lock=True)
        if draft.revision != body.expected_revision:
            raise DomainError(
                "draft_revision_conflict", "草稿已更新，请刷新后重新生成预览"
            )
        if draft.status != "READY":
            raise DomainError("draft_not_ready", "请等待当前草稿准备完成")
        value = body.delivery_config.model_dump(mode="json")
        if (draft.delivery_config or {}) == value:
            return draft.revision
        current = session.exec(
            select(DraftPreparation).where(
                DraftPreparation.tenant_id == context.tenant_id,
                DraftPreparation.draft_id == draft.id,
                DraftPreparation.draft_revision == draft.revision,
                DraftPreparation.status == "READY",
                col(DraftPreparation.phase) == "done",
            )
        ).one_or_none()
        if current is None:
            raise DomainError("draft_not_ready", "请等待当前草稿准备完成")
        draft.delivery_config = value
        # 每个 revision 只能有一个预览，排期变化必须换新 revision 使旧预览失效。
        # 排期不影响账户、链接、素材和场景核验，因此把已完成的准备结果原样
        # 迁到新 revision 并保持 READY，前端随后可立即生成预览，无需重新核验。
        revision = _bump_revision(session, draft, body.expected_revision)
        session.add(
            DraftPreparation(
                tenant_id=context.tenant_id,
                draft_id=draft.id,
                draft_revision=revision,
                actor_id=context.actor_id,
                request_id=uuid5(body.request_id, "delivery-preparation"),
                provider_task_id=current.provider_task_id,
                phase="done",
                status="READY",
            )
        )
        session.flush()
        return revision

    return _apply(
        session,
        context=context,
        draft_id=draft_id,
        request_id=body.request_id,
        kind="delivery",
        intent=body.model_dump(mode="json"),
        operation=apply,
    )
