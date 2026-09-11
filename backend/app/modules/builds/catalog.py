"""Tenant-scoped SQL pagination. Reads never schedule or advance preparation."""

from collections.abc import Mapping
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import aliased
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.core.pagination import Page
from app.modules.accounts.resolver import decode_cursor, encode_cursor
from app.modules.builds.drafts import get_draft
from app.modules.builds.models import (
    DraftAccount,
    DraftDrama,
    DraftGroupMaterial,
    DraftInput,
    DraftPreparation,
)
from app.modules.builds.schemas import (
    DraftDramaPublic,
    DraftInputPublic,
    DraftMaterialPublic,
    DraftSummary,
)
from app.modules.materials.models import MaterialFile
from app.modules.providers.schemas import display_config


def draft_summary(
    session: Session, *, context: TenantContext, draft_id: UUID
) -> DraftSummary:
    row = get_draft(session, context=context, draft_id=draft_id)
    counts: dict[str, dict[str, int]] = {"drama": {}, "account": {}}
    for kind, status, count in session.exec(
        select(DraftInput.kind, DraftInput.status, func.count())
        .where(
            DraftInput.tenant_id == context.tenant_id, DraftInput.draft_id == draft_id
        )
        .group_by(DraftInput.kind, DraftInput.status)
    ):
        counts[kind][status] = count
    prep = session.exec(
        select(DraftPreparation)
        .where(
            DraftPreparation.tenant_id == context.tenant_id,
            DraftPreparation.draft_id == draft_id,
        )
        .order_by(col(DraftPreparation.draft_revision).desc())
        .limit(1)
    ).first()
    drama_count = session.exec(
        select(func.count())
        .select_from(DraftDrama)
        .where(
            DraftDrama.tenant_id == context.tenant_id, DraftDrama.draft_id == draft_id
        )
    ).one()
    account_count = session.exec(
        select(func.count())
        .select_from(DraftAccount)
        .where(
            DraftAccount.tenant_id == context.tenant_id,
            DraftAccount.draft_id == draft_id,
        )
    ).one()
    return DraftSummary(
        draft_id=row.id,
        revision=row.revision,
        bc_id=row.bc_id,
        status=cast(Literal["DRAFT", "PREPARING", "READY", "BLOCKED"], row.status),
        strategy_version_id=row.strategy_version_id,
        provider_connection_id=row.provider_connection_id,
        execution_connection_id=row.execution_connection_id,
        application_id=row.application_id,
        link_config=display_config(row.link_config),
        input_counts=counts,
        drama_count=drama_count,
        account_count=account_count,
        task_id=prep.id if prep else None,
        provider_task_id=prep.provider_task_id if prep else None,
        error_code=prep.error_code
        if prep and prep.draft_revision == row.revision
        else None,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _page(
    scope: Mapping[str, str | None], cursor: str | None, limit: int
) -> str | None:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise DomainError("invalid_page_size", "每页最多 100 条")
    return decode_cursor(cursor, scope=scope)


def inputs_page(
    session: Session,
    *,
    context: TenantContext,
    draft_id: UUID,
    kind: Literal["drama", "account"],
    status: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> Page[DraftInputPublic]:
    draft = get_draft(session, context=context, draft_id=draft_id)
    scope = {
        "tenant": str(context.tenant_id),
        "draft": str(draft_id),
        "revision": str(draft.revision),
        "view": "inputs",
        "kind": kind,
        "status": status,
    }
    after = _page(scope, cursor, limit)
    try:
        number = int(after) if after else 0
        if number < 0:
            raise ValueError
    except ValueError:
        raise DomainError("invalid_cursor", "分页游标无效") from None
    query = select(DraftInput).where(
        DraftInput.tenant_id == context.tenant_id,
        DraftInput.draft_id == draft_id,
        DraftInput.kind == kind,
        DraftInput.line_no > number,
    )
    if status is not None:
        query = query.where(DraftInput.status == status)
    rows = session.exec(query.order_by(col(DraftInput.line_no)).limit(limit + 1)).all()
    return Page(
        items=[DraftInputPublic.model_validate(row) for row in rows[:limit]],
        next_cursor=encode_cursor(scope=scope, last_id=str(rows[limit - 1].line_no))
        if len(rows) > limit
        else None,
    )


def dramas_page(
    session: Session,
    *,
    context: TenantContext,
    draft_id: UUID,
    cursor: str | None = None,
    limit: int = 50,
) -> Page[DraftDramaPublic]:
    draft = get_draft(session, context=context, draft_id=draft_id)
    scope = {
        "tenant": str(context.tenant_id),
        "draft": str(draft_id),
        "revision": str(draft.revision),
        "view": "dramas",
    }
    after = _page(scope, cursor, limit)
    try:
        identity = UUID(after) if after else UUID(int=0)
    except ValueError:
        raise DomainError("invalid_cursor", "分页游标无效") from None
    rows = session.exec(
        select(DraftDrama)
        .where(
            DraftDrama.tenant_id == context.tenant_id,
            DraftDrama.draft_id == draft_id,
            DraftDrama.drama_id > identity,
        )
        .order_by(col(DraftDrama.drama_id))
        .limit(limit + 1)
    ).all()
    return Page(
        items=[DraftDramaPublic.model_validate(row) for row in rows[:limit]],
        next_cursor=encode_cursor(scope=scope, last_id=str(rows[limit - 1].drama_id))
        if len(rows) > limit
        else None,
    )


def materials_page(
    session: Session,
    *,
    context: TenantContext,
    draft_id: UUID,
    drama_id: UUID,
    cursor: str | None = None,
    limit: int = 50,
) -> Page[DraftMaterialPublic]:
    draft = get_draft(session, context=context, draft_id=draft_id)
    if session.get(DraftDrama, (context.tenant_id, draft.id, drama_id)) is None:
        raise DomainError("draft_not_found", "草稿剧目不存在")
    scope = {
        "tenant": str(context.tenant_id),
        "draft": str(draft_id),
        "revision": str(draft.revision),
        "drama": str(drama_id),
        "view": "materials",
    }
    after = _page(scope, cursor, limit)
    try:
        group, position = (int(part) for part in after.split(":")) if after else (0, 0)
        if min(group, position) < 0:
            raise ValueError
    except ValueError:
        raise DomainError("invalid_cursor", "分页游标无效") from None
    other = aliased(DraftGroupMaterial)
    shared = (
        select(other.material_id)
        .where(
            other.tenant_id == context.tenant_id,
            other.draft_id == draft_id,
            other.drama_id != drama_id,
            other.material_id == DraftGroupMaterial.material_id,
        )
        .exists()
    )
    query = (
        select(DraftGroupMaterial, MaterialFile.file_name, shared)
        .join(
            MaterialFile,
            and_(
                col(MaterialFile.tenant_id) == DraftGroupMaterial.tenant_id,
                col(MaterialFile.bc_id) == DraftGroupMaterial.bc_id,
                col(MaterialFile.id) == DraftGroupMaterial.material_id,
            ),
        )
        .where(
            DraftGroupMaterial.tenant_id == context.tenant_id,
            DraftGroupMaterial.draft_id == draft_id,
            DraftGroupMaterial.drama_id == drama_id,
            or_(
                col(DraftGroupMaterial.group_no) > group,
                and_(
                    col(DraftGroupMaterial.group_no) == group,
                    col(DraftGroupMaterial.position) > position,
                ),
            ),
        )
    )
    rows = session.exec(
        query.order_by(
            col(DraftGroupMaterial.group_no), col(DraftGroupMaterial.position)
        ).limit(limit + 1)
    ).all()
    return Page(
        items=[
            DraftMaterialPublic(
                material_id=row.material_id,
                file_name=name,
                group_no=row.group_no,
                position=row.position,
                shared_with_other_drama=bool(is_shared),
            )
            for row, name, is_shared in rows[:limit]
        ],
        next_cursor=encode_cursor(
            scope=scope,
            last_id=f"{rows[limit - 1][0].group_no}:{rows[limit - 1][0].position}",
        )
        if len(rows) > limit
        else None,
    )
