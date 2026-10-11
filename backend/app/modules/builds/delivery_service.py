"""草稿投放排期独立保存，保持已准备的账户、剧目、链接和素材分组。"""

from uuid import UUID, uuid5

from sqlmodel import Session

from app.core.context import TenantContext
from app.core.errors import DomainError

from .schemas import DeliveryChange


def save_delivery(
    session: Session, *, context: TenantContext, draft_id: UUID, body: DeliveryChange
) -> int:
    from .drafts import _bump_revision, get_draft, prepare_selected_mini
    from .mutations import _apply

    def apply() -> int:
        draft = get_draft(session, context=context, draft_id=draft_id, lock=True)
        if draft.revision != body.expected_revision:
            raise DomainError(
                "draft_revision_conflict", "草稿已更新，请刷新后重新保存排期"
            )
        if draft.status != "READY":
            raise DomainError("draft_not_ready", "请等待当前草稿准备完成")
        value = body.delivery_config.model_dump(mode="json")
        if (draft.delivery_config or {}) == value:
            return draft.revision
        draft.delivery_config = value
        draft.status = "DRAFT"
        # 排期写入预览，必须换新 revision 使旧预览失效；准备结果按 revision 归属，
        # 因此与定向一致复用场景重核流程沿用上一版准备，而非清空输入的通用更新。
        revision = _bump_revision(session, draft, body.expected_revision)
        prepare_selected_mini(
            session,
            context=context,
            draft_id=draft.id,
            request_id=uuid5(body.request_id, "delivery-scenes"),
        )
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
