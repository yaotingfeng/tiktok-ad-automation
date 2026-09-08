"""Transactional preparation in bounded pages; callers commit local progress."""

import hashlib
import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4, uuid5

from sqlalchemy import and_, delete, or_, text
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.core.pagination import Page
from app.modules.accounts.access import resolve_account_access
from app.modules.accounts.models import TenantBC
from app.modules.accounts.resolver import resolve_lines
from app.modules.accounts.schemas import InputLine
from app.modules.builds.models import (
    BuildDraft,
    DraftAccount,
    DraftDrama,
    DraftGroupMaterial,
    DraftInput,
    DraftPreparation,
    DraftPreparationRequest,
)
from app.modules.materials.models import AccountMaterial, MaterialFile
from app.modules.materials.service import match_materials
from app.modules.providers.models import LinkPreparation, PromotionLink
from app.modules.providers.repository import get_application
from app.modules.providers.schemas import _validate_json
from app.modules.providers.service import get_link_results, prepare_links
from app.modules.strategies.models import Strategy
from app.modules.strategies.service import get_version, get_version_record
from app.modules.tenants.permissions import require_tenant

# Engineering request bounds, independent of TikTok limits. Account processing
# remains paged even when the draft contains tens of thousands of pasted lines.
MAX_ACCOUNT_LINES = 100_000
MAX_INPUT_BYTES = 16 * 1024 * 1024
PAGE_SIZE = 100


def material_visible() -> ColumnElement[bool]:
    asset = (
        select(AccountMaterial.id)
        .where(
            AccountMaterial.tenant_id == MaterialFile.tenant_id,
            AccountMaterial.bc_id == MaterialFile.bc_id,
            AccountMaterial.material_id == MaterialFile.id,
            AccountMaterial.status == "available",
            col(AccountMaterial.verified_at).is_not(None),
        )
        .exists()
    )
    return (col(MaterialFile.storage_state) != "receiving") & or_(
        col(MaterialFile.storage_state) == "stored", asset
    )


def collect_pages[T](fetch: Callable[[str | None], Page[T]]) -> Iterator[T]:
    cursor, seen = None, set()
    while True:
        page = fetch(cursor)
        yield from page.items
        cursor = page.next_cursor
        if cursor is None:
            return
        if cursor in seen:
            raise DomainError("repeated_cursor", "资源分页游标重复，请重新准备")
        seen.add(cursor)


def _authorize(session: Session, context: TenantContext, action: str = "build") -> None:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action=action
    )


def get_draft(
    session: Session, *, context: TenantContext, draft_id: UUID, lock: bool = False
) -> BuildDraft:
    _authorize(session, context, "build" if lock else "read")
    query = select(BuildDraft).where(
        BuildDraft.tenant_id == context.tenant_id, BuildDraft.id == draft_id
    )
    if lock:
        query = query.with_for_update()
    row = session.exec(query.execution_options(populate_existing=True)).one_or_none()
    if row is None:
        raise DomainError("draft_not_found", "当前租户搭建草稿不存在")
    if lock:
        _authorize(session, context)
    return row


def _check_intent(
    session: Session,
    context: TenantContext,
    *,
    bc_id: str,
    strategy_version_id: UUID,
    provider_connection_id: UUID,
    application_id: str,
    drama_lines: list[str],
    account_lines: list[str],
    link_config: dict[str, Any],
) -> None:
    _authorize(session, context)
    if (
        not isinstance(bc_id, str)
        or not bc_id.strip()
        or len(bc_id) > 128
        or not isinstance(application_id, str)
        or not application_id.strip()
        or len(application_id) > 255
    ):
        raise DomainError("draft_input_invalid", "请选择当前 BC 和版权方应用")
    bc = session.get(TenantBC, (context.tenant_id, bc_id), populate_existing=True)
    if bc is None or bc.ownership_conflict:
        raise DomainError("account_not_in_bc", "当前租户 BC 不可用")
    version = get_version_record(
        session, context=context, version_id=strategy_version_id
    )
    strategy = session.get(Strategy, version.strategy_id, populate_existing=True)
    if strategy is None or not strategy.active:
        raise DomainError("strategy_not_found", "投放策略已停用或不存在")
    get_application(
        session,
        context=context,
        connection_id=provider_connection_id,
        application_id=application_id,
    )
    for values, bound in ((drama_lines, 1000), (account_lines, MAX_ACCOUNT_LINES)):
        if (
            not isinstance(values, list)
            or len(values) > bound
            or any(not isinstance(line, str) or len(line) > 1000 for line in values)
        ):
            raise DomainError(
                "draft_input_invalid",
                "剧名最多 1000 行、账户最多 100000 行，每行最多 1000 字符",
            )
    if (
        sum(len(line.encode()) for line in (*drama_lines, *account_lines))
        > MAX_INPUT_BYTES
    ):
        raise DomainError("draft_input_invalid", "粘贴输入总大小不能超过 16 MiB")
    try:
        if not isinstance(link_config, dict):
            raise ValueError
        _validate_json(link_config)
        if len(json.dumps(link_config, ensure_ascii=False).encode()) > 65536:
            raise ValueError
    except ValueError, TypeError, RecursionError:
        raise DomainError("draft_input_invalid", "推广链接配置无效") from None


def _store_inputs(
    session: Session, draft: BuildDraft, kind: str, lines: list[str]
) -> None:
    seen: dict[str, int] = {}
    for number, raw in enumerate(lines, 1):
        value = raw.strip()
        duplicate = seen.get(value) if value else None
        status = "empty" if not value else "duplicate" if duplicate else "pending"
        session.add(
            DraftInput(
                tenant_id=draft.tenant_id,
                draft_id=draft.id,
                kind=kind,
                line_no=number,
                raw_text=raw,
                status=status,
                duplicate_of=duplicate,
            )
        )
        if value:
            seen.setdefault(value, number)
        if number % 500 == 0:
            session.flush()


def create_draft(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    strategy_version_id: UUID,
    provider_connection_id: UUID,
    application_id: str,
    drama_lines: list[str],
    account_lines: list[str],
    link_config: dict[str, Any],
    request_id: UUID | None = None,
) -> UUID:
    intent: dict[str, Any] = {
        "bc_id": bc_id,
        "strategy_version_id": strategy_version_id,
        "provider_connection_id": provider_connection_id,
        "application_id": application_id,
        "drama_lines": drama_lines,
        "account_lines": account_lines,
        "link_config": link_config,
    }
    _check_intent(session, context, **intent)
    request_id = request_id or uuid4()
    digest = hashlib.sha256(
        json.dumps(
            intent,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"draft:{context.tenant_id}:{request_id}"},
    )
    _authorize(session, context)
    prior = session.exec(
        select(BuildDraft).where(
            BuildDraft.tenant_id == context.tenant_id,
            BuildDraft.request_id == request_id,
        )
    ).one_or_none()
    if prior:
        if prior.request_digest != digest:
            raise DomainError("idempotency_conflict", "同一请求标识已用于不同草稿输入")
        return prior.id
    draft = BuildDraft(
        tenant_id=context.tenant_id,
        bc_id=bc_id,
        strategy_version_id=strategy_version_id,
        provider_connection_id=provider_connection_id,
        application_id=application_id,
        link_config=link_config,
        created_by=context.actor_id,
        request_id=request_id,
        request_digest=digest,
    )
    session.add(draft)
    session.flush()
    _store_inputs(session, draft, "drama", drama_lines)
    _store_inputs(session, draft, "account", account_lines)
    session.flush()
    return draft.id


def prepare_draft(
    session: Session, *, context: TenantContext, draft_id: UUID, request_id: UUID
) -> UUID:
    from app.modules.builds.draft_tasks import queue_preparation

    _authorize(session, context)
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"draft-prepare:{context.tenant_id}:{request_id}"},
    )
    draft = get_draft(session, context=context, draft_id=draft_id, lock=True)
    prior = session.get(DraftPreparationRequest, (context.tenant_id, request_id))
    if prior:
        if prior.draft_id != draft.id:
            raise DomainError("idempotency_conflict", "请求标识已用于其他草稿")
        return prior.preparation_id
    existing = session.exec(
        select(DraftPreparation).where(
            DraftPreparation.tenant_id == context.tenant_id,
            DraftPreparation.draft_id == draft.id,
            DraftPreparation.draft_revision == draft.revision,
        )
    ).one_or_none()
    if existing and existing.status == "PENDING":
        session.add(
            DraftPreparationRequest(
                tenant_id=context.tenant_id,
                request_id=request_id,
                draft_id=draft.id,
                preparation_id=existing.id,
            )
        )
        session.flush()
        return existing.id
    if existing or draft.status != "DRAFT":
        _bump_revision(session, draft, draft.revision)
    # Every new local preparation starts account resolution afresh. Manual
    # groups survive while automatic groups are rebuilt in stable order.
    session.execute(
        delete(DraftAccount).where(
            col(DraftAccount.tenant_id) == context.tenant_id,
            col(DraftAccount.draft_id) == draft.id,
        )
    )
    dramas = session.exec(
        select(DraftDrama).where(
            DraftDrama.tenant_id == context.tenant_id,
            DraftDrama.draft_id == draft.id,
        )
    ).all()
    for drama in dramas:
        if drama.material_state == "manual":
            continue
        session.execute(
            delete(DraftGroupMaterial).where(
                col(DraftGroupMaterial.tenant_id) == context.tenant_id,
                col(DraftGroupMaterial.draft_id) == draft.id,
                col(DraftGroupMaterial.drama_id) == drama.drama_id,
            )
        )
        drama.material_state, drama.material_cursor, drama.matched_count = (
            "pending",
            None,
            0,
        )
        session.add(drama)
    lines = session.exec(
        select(DraftInput.raw_text)
        .where(
            DraftInput.tenant_id == context.tenant_id,
            DraftInput.draft_id == draft.id,
            DraftInput.kind == "drama",
        )
        .order_by(col(DraftInput.line_no))
    ).all()
    if not any(line.strip() for line in lines):
        raise DomainError("draft_inputs_empty", "至少输入一部剧目后再解析准备")
    prep = DraftPreparation(
        tenant_id=context.tenant_id,
        draft_id=draft.id,
        draft_revision=draft.revision,
        actor_id=context.actor_id,
        request_id=request_id,
    )
    from app.modules.providers.link_steps import _digest

    provider_digest = _digest(
        [
            str(draft.provider_connection_id),
            draft.application_id,
            list(lines),
            draft.link_config,
        ]
    )
    previous_provider = session.exec(
        select(LinkPreparation.id)
        .join(
            DraftPreparation,
            and_(
                col(DraftPreparation.tenant_id) == LinkPreparation.tenant_id,
                col(DraftPreparation.provider_task_id) == LinkPreparation.id,
            ),
        )
        .where(
            DraftPreparation.tenant_id == context.tenant_id,
            DraftPreparation.draft_id == draft.id,
            LinkPreparation.connection_id == draft.provider_connection_id,
            LinkPreparation.application_id == draft.application_id,
            LinkPreparation.request_digest == provider_digest,
        )
        .order_by(col(DraftPreparation.draft_revision).desc())
        .limit(1)
    ).first()
    prep.provider_task_id = (
        previous_provider
        if previous_provider is not None
        else prepare_links(
            session,
            context=context,
            connection_id=draft.provider_connection_id,
            application_id=draft.application_id,
            lines=list(lines),
            config=draft.link_config,
            request_id=uuid5(prep.id, "provider-links"),
        )
    )
    session.add(prep)
    draft.status = "PREPARING"
    session.add(draft)
    session.flush()
    session.add(
        DraftPreparationRequest(
            tenant_id=context.tenant_id,
            request_id=request_id,
            draft_id=draft.id,
            preparation_id=prep.id,
        )
    )
    session.flush()
    queue_preparation(session, prep, delay=0)
    return prep.id


def _accounts_page(
    session: Session, context: TenantContext, draft: BuildDraft, prep: DraftPreparation
) -> None:
    rows = session.exec(
        select(DraftInput)
        .where(
            DraftInput.tenant_id == context.tenant_id,
            DraftInput.draft_id == draft.id,
            DraftInput.kind == "account",
            DraftInput.line_no > prep.account_after,
        )
        .order_by(col(DraftInput.line_no))
        .limit(PAGE_SIZE)
    ).all()
    if not rows:
        prep.phase = "links"
        return
    results = resolve_lines(
        session,
        context=context,
        bc_id=draft.bc_id,
        lines=[InputLine(line_no=row.line_no, raw=row.raw_text) for row in rows],
    )
    for row, result in zip(rows, results, strict=True):
        row.status, row.reason_code = result.status.lower(), result.reason
        row.advertiser_id, row.duplicate_of = result.advertiser_id, result.duplicate_of
        row.candidates = [{"advertiser_id": value} for value in result.candidates]
        if result.status in {"MATCHED", "DUPLICATE"}:
            assert result.advertiser_id
            prior = session.get(
                DraftAccount, (context.tenant_id, draft.id, result.advertiser_id)
            )
            if prior:
                row.status, row.duplicate_of = "duplicate", prior.first_line
            elif result.status == "MATCHED":
                access = resolve_account_access(
                    session,
                    context=context,
                    bc_id=draft.bc_id,
                    advertiser_id=result.advertiser_id,
                    action="build",
                )
                session.add(
                    DraftAccount(
                        tenant_id=context.tenant_id,
                        draft_id=draft.id,
                        bc_id=draft.bc_id,
                        advertiser_id=access.advertiser_id,
                        connection_id=access.connection_id,
                        currency=access.currency,
                        timezone=access.timezone,
                        first_line=row.line_no,
                    )
                )
        session.add(row)
    prep.account_after = rows[-1].line_no


def _links_page(
    session: Session, context: TenantContext, draft: BuildDraft, prep: DraftPreparation
) -> None:
    assert prep.provider_task_id
    page = get_link_results(
        session,
        context=context,
        task_id=prep.provider_task_id,
        cursor=prep.link_cursor,
        page_size=PAGE_SIZE,
    )
    for result in page.items:
        row = session.exec(
            select(DraftInput).where(
                DraftInput.tenant_id == context.tenant_id,
                DraftInput.draft_id == draft.id,
                DraftInput.kind == "drama",
                DraftInput.line_no == result.line_no,
            )
        ).one()
        row.status, row.reason_code, row.provider_input_id = (
            result.status,
            result.error_code,
            result.input_id,
        )
        row.drama_id = result.drama_id
        row.candidates = [
            candidate.model_dump(mode="json") for candidate in result.candidates
        ]
        if result.status in {"pending", "retryable_error"}:
            prep.pending_links = True
        if result.status == "ready":
            assert result.drama_id and result.link_id and result.title
            # DTOs do not weaken the persisted identity boundary.
            link = session.exec(
                select(PromotionLink).where(
                    PromotionLink.tenant_id == context.tenant_id,
                    PromotionLink.id == result.link_id,
                    PromotionLink.drama_id == result.drama_id,
                    PromotionLink.connection_id == draft.provider_connection_id,
                    PromotionLink.application_id == draft.application_id,
                    PromotionLink.status == "ready",
                )
            ).one_or_none()
            if link is None:
                raise DomainError("draft_link_invalid", "推广链接已变化，请重新准备")
            prior = session.get(
                DraftDrama, (context.tenant_id, draft.id, result.drama_id)
            )
            if prior is None:
                session.add(
                    DraftDrama(
                        tenant_id=context.tenant_id,
                        draft_id=draft.id,
                        bc_id=draft.bc_id,
                        drama_id=result.drama_id,
                        link_id=result.link_id,
                        title=result.title,
                        first_line=result.line_no,
                    )
                )
            elif prior.first_line != result.line_no:
                row.status, row.duplicate_of = "duplicate", prior.first_line
        session.add(row)
    if page.next_cursor is not None and page.next_cursor == prep.link_cursor:
        raise DomainError("repeated_cursor", "版权方分页游标重复")
    prep.link_cursor = page.next_cursor
    if page.next_cursor is None:
        prep.phase = "materials"


def _materials_page(
    session: Session, context: TenantContext, draft: BuildDraft, prep: DraftPreparation
) -> None:
    drama = session.exec(
        select(DraftDrama)
        .where(
            DraftDrama.tenant_id == context.tenant_id,
            DraftDrama.draft_id == draft.id,
            col(DraftDrama.material_state).in_(["pending", "matching"]),
        )
        .order_by(col(DraftDrama.first_line), col(DraftDrama.drama_id))
        .limit(1)
    ).first()
    if drama is None:
        if prep.pending_links:
            prep.pending_links = False
            prep.phase = "links"
        else:
            prep.status, prep.phase, draft.status = "READY", "done", "READY"
        return
    page = match_materials(
        session,
        context=context,
        bc_id=draft.bc_id,
        title=drama.title,
        cursor=drama.material_cursor,
    )
    config = get_version(session, context=context, version_id=draft.strategy_version_id)
    for item in page.items:
        # The shared library may gain a file between pages. The unique reference
        # keeps retries idempotent; final preview freezes only persisted rows.
        exists = session.exec(
            select(DraftGroupMaterial.material_id).where(
                col(DraftGroupMaterial.tenant_id) == context.tenant_id,
                col(DraftGroupMaterial.draft_id) == draft.id,
                col(DraftGroupMaterial.drama_id) == drama.drama_id,
                DraftGroupMaterial.material_id == item.material_id,
            )
        ).first()
        if exists is not None:
            continue
        index = drama.matched_count
        session.add(
            DraftGroupMaterial(
                tenant_id=context.tenant_id,
                draft_id=draft.id,
                drama_id=drama.drama_id,
                bc_id=draft.bc_id,
                group_no=index // config.group_size + 1,
                position=index % config.group_size + 1,
                material_id=item.material_id,
            )
        )
        drama.matched_count += 1
    if page.next_cursor is not None and page.next_cursor == drama.material_cursor:
        raise DomainError("repeated_cursor", "素材分页游标重复")
    drama.material_cursor = page.next_cursor
    drama.material_state = "matching" if page.next_cursor else "ready"
    session.add(drama)


def continue_draft(session: Session, *, context: TenantContext, task_id: UUID) -> bool:
    prep = session.exec(
        select(DraftPreparation)
        .where(
            DraftPreparation.tenant_id == context.tenant_id,
            DraftPreparation.id == task_id,
        )
        .execution_options(populate_existing=True)
    ).one_or_none()
    if prep is None:
        raise DomainError("draft_not_found", "草稿准备任务不存在")
    draft = get_draft(session, context=context, draft_id=prep.draft_id, lock=True)
    prep = session.exec(
        select(DraftPreparation)
        .where(DraftPreparation.id == prep.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    if prep.status != "PENDING":
        return True
    if prep.draft_revision != draft.revision:
        prep.status = "OBSOLETE"
    elif prep.phase == "accounts":
        _accounts_page(session, context, draft, prep)
    elif prep.phase == "links":
        _links_page(session, context, draft, prep)
    elif prep.phase == "materials":
        _materials_page(session, context, draft, prep)
    draft.updated_at = datetime.now(UTC)
    session.add_all([prep, draft])
    session.flush()
    return prep.status != "PENDING"


def _bump_revision(session: Session, draft: BuildDraft, expected_revision: int) -> int:
    if type(expected_revision) is not int or draft.revision != expected_revision:
        raise DomainError(
            "draft_revision_conflict", "草稿已更新，请保留当前编辑并读取最新版本"
        )
    draft.revision += 1
    draft.updated_at = datetime.now(UTC)
    session.add(draft)
    return draft.revision


def edit_material_groups(
    session: Session,
    *,
    context: TenantContext,
    draft_id: UUID,
    drama_id: UUID,
    expected_revision: int,
    groups: list[list[UUID]],
) -> int:
    draft = get_draft(session, context=context, draft_id=draft_id, lock=True)
    if draft.revision != expected_revision:
        raise DomainError(
            "draft_revision_conflict", "草稿已更新，请保留当前编辑并读取最新版本"
        )
    drama = session.get(
        DraftDrama, (context.tenant_id, draft.id, drama_id), populate_existing=True
    )
    if (
        draft.status != "READY"
        or drama is None
        or drama.material_state not in {"ready", "manual"}
    ):
        raise DomainError("draft_not_ready", "等待当前草稿准备完成后再调整素材")
    if (
        not isinstance(groups, list)
        or len(groups) > 100_000
        or any(not isinstance(group, list) or not group for group in groups)
    ):
        raise DomainError("draft_groups_invalid", "素材组不能为空组")
    identities = [identity for group in groups for identity in group]
    if (
        len(identities) > 100_000
        or any(not isinstance(identity, UUID) for identity in identities)
        or len(set(identities)) != len(identities)
    ):
        raise DomainError("draft_groups_invalid", "同一剧目不能重复使用同一素材")
    for offset in range(0, len(identities), PAGE_SIZE):
        chunk = identities[offset : offset + PAGE_SIZE]
        actual = session.exec(
            select(MaterialFile.id).where(
                MaterialFile.tenant_id == context.tenant_id,
                MaterialFile.bc_id == draft.bc_id,
                col(MaterialFile.id).in_(chunk),
                material_visible(),
            )
        ).all()
        if set(actual) != set(chunk):
            raise DomainError("material_not_found", "素材不在当前租户 BC 可用素材库")
    revision = _bump_revision(session, draft, expected_revision)
    session.execute(
        delete(DraftGroupMaterial).where(
            col(DraftGroupMaterial.tenant_id) == context.tenant_id,
            col(DraftGroupMaterial.draft_id) == draft.id,
            col(DraftGroupMaterial.drama_id) == drama_id,
        )
    )
    for group_no, group in enumerate(groups, 1):
        for position, identity in enumerate(group, 1):
            session.add(
                DraftGroupMaterial(
                    tenant_id=context.tenant_id,
                    draft_id=draft.id,
                    drama_id=drama_id,
                    bc_id=draft.bc_id,
                    group_no=group_no,
                    position=position,
                    material_id=identity,
                )
            )
        if group_no % PAGE_SIZE == 0:
            session.flush()
    drama.matched_count, drama.material_state, drama.material_cursor = (
        len(identities),
        "manual",
        None,
    )
    session.add(drama)
    session.flush()
    return revision


def update_draft(
    session: Session,
    *,
    context: TenantContext,
    draft_id: UUID,
    expected_revision: int,
    strategy_version_id: UUID | None = None,
    provider_connection_id: UUID | None = None,
    application_id: str | None = None,
    drama_lines: list[str] | None = None,
    account_lines: list[str] | None = None,
    link_config: dict[str, Any] | None = None,
) -> int:
    """Input/strategy changes restart preparation; material-only edits use their
    separate endpoint to preserve every other drama's prepared grouping.
    """
    draft = get_draft(session, context=context, draft_id=draft_id, lock=True)
    if draft.revision != expected_revision:
        raise DomainError(
            "draft_revision_conflict", "草稿已更新，请保留当前编辑并读取最新版本"
        )
    existing = session.exec(
        select(DraftInput.kind, DraftInput.raw_text)
        .where(
            DraftInput.tenant_id == context.tenant_id, DraftInput.draft_id == draft.id
        )
        .order_by(col(DraftInput.kind), col(DraftInput.line_no))
    ).all()
    old_intent: dict[str, Any] = {
        "bc_id": draft.bc_id,
        "strategy_version_id": draft.strategy_version_id,
        "provider_connection_id": draft.provider_connection_id,
        "application_id": draft.application_id,
        "drama_lines": [raw for kind, raw in existing if kind == "drama"],
        "account_lines": [raw for kind, raw in existing if kind == "account"],
        "link_config": draft.link_config,
    }
    intent: dict[str, Any] = old_intent | {
        key: value
        for key, value in {
            "strategy_version_id": strategy_version_id,
            "provider_connection_id": provider_connection_id,
            "application_id": application_id,
            "drama_lines": drama_lines,
            "account_lines": account_lines,
            "link_config": link_config,
        }.items()
        if value is not None
    }
    _check_intent(session, context, **intent)
    if intent == old_intent:
        return draft.revision
    revision = _bump_revision(session, draft, expected_revision)
    for model in (DraftGroupMaterial, DraftDrama, DraftAccount, DraftInput):
        session.execute(
            delete(model).where(
                col(model.tenant_id) == context.tenant_id,
                col(model.draft_id) == draft.id,
            )
        )
    draft.strategy_version_id = intent["strategy_version_id"]
    draft.provider_connection_id = intent["provider_connection_id"]
    draft.application_id = intent["application_id"]
    draft.link_config = intent["link_config"]
    draft.status = "DRAFT"
    session.add(draft)
    session.flush()
    _store_inputs(session, draft, "drama", intent["drama_lines"])
    _store_inputs(session, draft, "account", intent["account_lines"])
    session.flush()
    return revision
