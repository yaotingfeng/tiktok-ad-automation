"""Scoped reads for provider modules; no network calls or implicit commits.

Preparation creation and effect claims are implemented by their later workflow
steps. These reads always reload tenant membership and constrain every identity
by tenant/connection/application, including when a caller already holds an ORM
instance in the Session identity map.
"""

import base64
import binascii
import json
from typing import Any
from uuid import UUID, uuid4

from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.core.pagination import Page
from app.modules.providers.models import (
    LinkPreparation,
    PromotionLink,
    ProviderApplication,
    ProviderConnection,
    ProviderDrama,
    ProviderEffect,
)
from app.modules.providers.schemas import ResolvedLink, link_reuse_key
from app.modules.tenants.permissions import require_tenant


def get_connection(
    session: Session, *, context: TenantContext, connection_id: UUID
) -> ProviderConnection:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    result = session.exec(
        select(ProviderConnection)
        .where(
            ProviderConnection.tenant_id == context.tenant_id,
            ProviderConnection.id == connection_id,
        )
        .execution_options(populate_existing=True)
    ).one_or_none()
    if result is None:
        raise DomainError("resource_not_found", "当前租户版权方连接不存在")
    return result


def get_application(
    session: Session,
    *,
    context: TenantContext,
    connection_id: UUID,
    application_id: str,
) -> ProviderApplication:
    get_connection(session, context=context, connection_id=connection_id)
    result = session.exec(
        select(ProviderApplication)
        .where(
            ProviderApplication.tenant_id == context.tenant_id,
            ProviderApplication.connection_id == connection_id,
            ProviderApplication.external_id == application_id,
        )
        .execution_options(populate_existing=True)
    ).one_or_none()
    if result is None:
        raise DomainError("resource_not_found", "当前连接版权方应用不存在")
    return result


def _cursor(scope: dict[str, Any], last_id: UUID) -> str:
    payload = json.dumps(
        {"scope": scope, "last_id": str(last_id)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(payload.encode()).decode()


def _after_id(cursor: str | None, scope: dict[str, Any]) -> UUID | None:
    if cursor is None:
        return None
    try:
        if len(cursor) > 8192:
            raise ValueError("cursor length")
        raw = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if (
            not isinstance(raw, dict)
            or set(raw) != {"scope", "last_id"}
            or raw["scope"] != scope
        ):
            raise ValueError("cursor scope")
        return UUID(raw["last_id"])
    except ValueError, TypeError, KeyError, AttributeError, binascii.Error:
        raise DomainError("invalid_cursor", "游标不属于当前版权方查询") from None


def _limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise DomainError("configuration_invalid", "分页数量必须在 1 到 200 之间")


def list_dramas(
    session: Session,
    *,
    context: TenantContext,
    connection_id: UUID,
    application_id: str,
    title: str | None = None,
    cursor: str | None = None,
    limit: int = 100,
) -> Page[ProviderDrama]:
    """Exact local title matches, with a stable scope-bound UUID seek cursor."""
    get_application(
        session,
        context=context,
        connection_id=connection_id,
        application_id=application_id,
    )
    _limit(limit)
    scope = {
        "tenant_id": str(context.tenant_id),
        "connection_id": str(connection_id),
        "application_id": application_id,
        "title": title,
    }
    after_id = _after_id(cursor, scope)
    statement = select(ProviderDrama).where(
        ProviderDrama.tenant_id == context.tenant_id,
        ProviderDrama.connection_id == connection_id,
        ProviderDrama.application_id == application_id,
    )
    if title is not None:
        statement = statement.where(ProviderDrama.title == title)
    if after_id is not None:
        statement = statement.where(col(ProviderDrama.id) > after_id)
    rows = list(
        session.exec(
            statement.order_by(col(ProviderDrama.id))
            .limit(limit + 1)
            .execution_options(populate_existing=True)
        ).all()
    )
    return Page[ProviderDrama](
        items=rows[:limit],
        next_cursor=_cursor(scope, rows[limit - 1].id) if len(rows) > limit else None,
    )


def find_ready_link(
    session: Session,
    *,
    context: TenantContext,
    connection_id: UUID,
    application_id: str,
    external_drama_id: str,
    config: dict[str, Any],
) -> PromotionLink | None:
    get_application(
        session,
        context=context,
        connection_id=connection_id,
        application_id=application_id,
    )
    key = link_reuse_key(
        context.tenant_id, connection_id, application_id, external_drama_id, config
    )
    # Join the recorded identity as well as the key, so a malformed/stale key in a
    # stored row cannot make another drama appear reusable under this request.
    return session.exec(
        select(PromotionLink)
        .join(
            ProviderDrama,
            (
                (col(PromotionLink.tenant_id) == col(ProviderDrama.tenant_id))
                & (col(PromotionLink.connection_id) == col(ProviderDrama.connection_id))
                & (
                    col(PromotionLink.application_id)
                    == col(ProviderDrama.application_id)
                )
                & (col(PromotionLink.drama_id) == col(ProviderDrama.id))
            ),
        )
        .where(
            PromotionLink.tenant_id == context.tenant_id,
            PromotionLink.connection_id == connection_id,
            PromotionLink.application_id == application_id,
            PromotionLink.reuse_key == key,
            PromotionLink.status == "ready",
            col(PromotionLink.config) == config,
            ProviderDrama.external_drama_id == external_drama_id,
        )
        .execution_options(populate_existing=True)
    ).one_or_none()


# Workflow helpers only flush; orchestration owns short transaction boundaries.
def claim_remote_scope(
    session: Session, *, tenant_id: UUID, scope_key: str, item_id: UUID
) -> bool:
    from sqlalchemy.dialects.postgresql import insert

    from app.modules.providers.models import ProviderRemoteScope

    session.exec(
        insert(ProviderRemoteScope)
        .values(
            id=uuid4(),
            tenant_id=tenant_id,
            scope_key=scope_key,
            status="idle",
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "scope_key"])
    )
    scope = session.exec(
        select(ProviderRemoteScope)
        .where(
            ProviderRemoteScope.tenant_id == tenant_id,
            ProviderRemoteScope.scope_key == scope_key,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    if scope.active_item_id is not None and scope.active_item_id != item_id:
        return False
    if scope.active_item_id is None:
        scope.active_item_id = item_id
        scope.status = "held"
        session.add(scope)
        session.flush()
    return True


def get_or_create_effect(
    session: Session, *, tenant_id: UUID, scope_key: str, step: str, request_digest: str
) -> ProviderEffect:
    from sqlalchemy.dialects.postgresql import insert

    from app.modules.providers.models import ProviderEffect

    session.exec(
        insert(ProviderEffect)
        .values(
            id=uuid4(),
            tenant_id=tenant_id,
            remote_scope_key=scope_key,
            step=step,
            request_digest=request_digest,
            status="pending",
            result={},
        )
        .on_conflict_do_nothing(
            index_elements=["tenant_id", "remote_scope_key", "step", "request_digest"]
        )
    )
    return session.exec(
        select(ProviderEffect)
        .where(
            ProviderEffect.tenant_id == tenant_id,
            ProviderEffect.remote_scope_key == scope_key,
            ProviderEffect.step == step,
            ProviderEffect.request_digest == request_digest,
        )
        .execution_options(populate_existing=True)
    ).one()


def create_preparation_request(
    session: Session,
    *,
    context: TenantContext,
    connection_id: UUID,
    application_id: str,
    lines: list[str],
    config: dict[str, Any],
    request_id: UUID,
) -> UUID:
    from sqlalchemy.dialects.postgresql import insert

    from app.modules.providers.link_steps import _digest, _result
    from app.modules.providers.models import LinkPreparation, LinkPreparationItem
    from app.modules.providers.schemas import _validate_json
    from app.modules.providers.service import clean_lines
    from app.modules.providers.tasks import queue_item

    require_tenant(
        session,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        action="provider_write",
    )
    get_application(
        session,
        context=context,
        connection_id=connection_id,
        application_id=application_id,
    )
    connection = get_connection(session, context=context, connection_id=connection_id)
    cleaned = clean_lines(lines)
    if not cleaned or not isinstance(config, dict):
        raise DomainError("provider_request_invalid", "剧名和推广配置不能为空")
    try:
        _validate_json(config)
        digest = _digest([str(connection_id), application_id, lines, config])
    except ValueError, TypeError:
        raise DomainError("provider_request_invalid", "推广配置无效") from None
    preparation_id = uuid4()
    inserted = session.exec(
        insert(LinkPreparation)
        .values(
            id=preparation_id,
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            request_id=request_id,
            request_digest=digest,
            connection_id=connection_id,
            application_id=application_id,
            config=config,
            status="pending",
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "request_id"])
        .returning(col(LinkPreparation.id))
    ).first()
    if inserted is None:
        existing = session.exec(
            select(LinkPreparation).where(
                LinkPreparation.tenant_id == context.tenant_id,
                LinkPreparation.request_id == request_id,
            )
        ).one()
        if existing.request_digest != digest:
            raise DomainError("request_id_conflict", "同一请求标识已用于不同输入")
        return existing.id
    prep = session.get(LinkPreparation, preparation_id)
    assert prep is not None
    for number, value in cleaned:
        item = LinkPreparationItem(
            tenant_id=context.tenant_id,
            preparation_id=prep.id,
            line_no=number,
            raw_input=value,
        )
        item.resolved = _result(item, prep, connection, {}, status="pending")
        session.add(item)
        session.flush()
        queue_item(session, item, actor_id=prep.actor_id, revision=0)
    return preparation_id


def _preparation(
    session: Session, context: TenantContext, task_id: UUID
) -> LinkPreparation:
    from app.modules.providers.models import LinkPreparation

    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    prep = session.exec(
        select(LinkPreparation).where(
            LinkPreparation.tenant_id == context.tenant_id,
            LinkPreparation.id == task_id,
        )
    ).one_or_none()
    if prep is None:
        raise DomainError("resource_not_found", "当前租户准备任务不存在")
    get_application(
        session,
        context=context,
        connection_id=prep.connection_id,
        application_id=prep.application_id,
    )
    return prep


def read_preparation_results(
    session: Session,
    *,
    context: TenantContext,
    task_id: UUID,
    cursor: str | None = None,
    page_size: int = 100,
) -> Page[ResolvedLink]:
    from sqlalchemy import and_, or_

    from app.modules.providers.models import LinkPreparationItem
    from app.modules.providers.schemas import ResolvedLink

    _preparation(session, context, task_id)
    if type(page_size) is not int or page_size not in {50, 100}:
        raise DomainError("invalid_page_size", "每页仅支持 50 或 100 行")
    scope = {
        "tenant_id": str(context.tenant_id),
        "task_id": str(task_id),
        "kind": "results",
    }
    statement = select(LinkPreparationItem).where(
        LinkPreparationItem.tenant_id == context.tenant_id,
        LinkPreparationItem.preparation_id == task_id,
    )
    if cursor is not None:
        try:
            if len(cursor) > 4096:
                raise ValueError
            payload = json.loads(
                base64.b64decode(cursor, altchars=b"-_", validate=True)
            )
            if (
                not isinstance(payload, dict)
                or set(payload) != {"scope", "line_no", "id"}
                or payload["scope"] != scope
            ):
                raise ValueError
            line_no, item_id = payload["line_no"], UUID(payload["id"])
            if type(line_no) is not int or line_no < 1 or str(item_id) != payload["id"]:
                raise ValueError
        except ValueError, TypeError, KeyError, AttributeError, binascii.Error:
            raise DomainError("invalid_cursor", "结果游标不属于本次任务") from None
        statement = statement.where(
            or_(
                col(LinkPreparationItem.line_no) > line_no,
                and_(
                    col(LinkPreparationItem.line_no) == line_no,
                    col(LinkPreparationItem.id) > item_id,
                ),
            )
        )
    rows = session.exec(
        statement.order_by(
            col(LinkPreparationItem.line_no), col(LinkPreparationItem.id)
        )
        .limit(page_size + 1)
        .execution_options(populate_existing=True)
    ).all()
    next_cursor = None
    if len(rows) > page_size:
        last = rows[page_size - 1]
        next_cursor = base64.urlsafe_b64encode(
            json.dumps(
                {
                    "scope": scope,
                    "line_no": last.line_no,
                    "id": str(last.id),
                },
                sort_keys=True,
            ).encode()
        ).decode()
    return Page[ResolvedLink](
        items=[
            ResolvedLink.model_validate(
                {key: value for key, value in row.resolved.items() if key != "_work"}
            )
            for row in rows[:page_size]
        ],
        next_cursor=next_cursor,
    )


def select_preparation_candidate(
    session: Session,
    *,
    context: TenantContext,
    input_id: UUID,
    external_drama_id: str,
) -> UUID:
    from app.modules.providers.link_steps import (
        _load,
        _resolved_drama,
        _store,
        _validated_work,
    )
    from app.modules.providers.schemas import DramaCandidate
    from app.modules.providers.tasks import queue_item

    require_tenant(
        session,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        action="provider_write",
    )
    item, prep, connection, _ = _load(session, context, input_id, lock=True)
    require_tenant(
        session,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        action="provider_write",
    )
    if item.status != "needs_resolution":
        raise DomainError("candidate_not_available", "当前输入行不需要候选选择")
    work = _validated_work(item.resolved.get("_work", {}))
    candidates = [
        DramaCandidate.model_validate(value) for value in work.get("candidates", [])
    ]
    selected = next(
        (
            candidate
            for candidate in candidates
            if candidate.external_drama_id == external_drama_id
        ),
        None,
    )
    if selected is None:
        raise DomainError("candidate_not_available", "候选不属于当前输入行及应用")
    work["drama"] = _resolved_drama(session, context, prep, selected.model_dump())
    work.update(stage="lookup", lookup_cursor=None, lookup_found=None)
    work.pop("claim_token", None)
    work.pop("claim_until", None)
    _store(item, prep, connection, work)
    prep.status = "running"
    session.add(item)
    session.add(prep)
    queue_item(
        session,
        item,
        actor_id=prep.actor_id,
        revision=work.get("dispatch_revision", 0) + 1,
    )
    return prep.id
