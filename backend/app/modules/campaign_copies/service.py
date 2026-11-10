"""聚合复制草稿只编排本地事实；真正平台创建仍由原搭建执行器负责。"""

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4, uuid5

from sqlalchemy import text
from sqlmodel import Session, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.modules.accounts.access import resolve_account_access
from app.modules.accounts.routing import freeze_route, verify_route
from app.modules.builds.preview_models import BuildPreview
from app.modules.campaign_copies.models import CampaignCopyDraft, CampaignCopyRequest
from app.modules.campaign_copies.schemas import (
    CopyConfiguration,
    CopyDraftPublic,
    CopyPatchRequest,
    CopyRevisionRequest,
    CopySubmitRequest,
)
from app.modules.reporting.selection import get_frozen_selection
from app.modules.tenants.permissions import require_tenant

MAX_SERIES = 1000


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


def get_draft(
    session: Session, *, context: TenantContext, draft_id: UUID, write: bool = False
) -> CampaignCopyDraft:
    require_tenant(
        session,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        action="build" if write else "read",
    )
    query = select(CampaignCopyDraft).where(
        CampaignCopyDraft.tenant_id == context.tenant_id,
        CampaignCopyDraft.id == draft_id,
    )
    if write:
        query = query.with_for_update()
    row = session.exec(query.execution_options(populate_existing=True)).one_or_none()
    if row is None:
        raise DomainError("copy_not_found", "复制草稿不存在")
    if write and row.actor_id != context.actor_id:
        raise DomainError("action_forbidden", "仅发起人可修改或提交复制草稿")
    route = FrozenTikTokRoute.model_validate(row.route)
    verify_route(
        session, context=context, route=route, advertiser_id=None, capability="read"
    )
    return row


PUBLIC_SOURCE = {
    "advertiser_id",
    "campaign_id",
    "name",
    "currency",
    "budget",
    "target_roas",
    "budget_strategy",
    "bid_strategy",
    "materials",
    "targeting",
    "blockers",
    "targeting_mixed",
    "group_settings",
    "provider_name",
    "application_name",
    "minis_id",
}


def public(row: CampaignCopyDraft) -> CopyDraftPublic:
    return CopyDraftPublic.model_validate(
        {
            "draft_id": row.id,
            "bc_id": row.bc_id,
            "revision": row.revision,
            "status": row.status,
            "rows": [
                {
                    "row_id": item["row_id"],
                    "source": {
                        k: v for k, v in item["source"].items() if k in PUBLIC_SOURCE
                    },
                    "configuration": item["configuration"],
                }
                for item in row.rows
            ],
            "preview_digest": row.preview_digest,
            "children": row.children,
            "submission_ids": list(
                dict.fromkeys(
                    UUID(c["submission_id"])
                    for c in row.children
                    if c.get("submission_id")
                )
            ),
            "error_code": row.error_code,
        }
    )


def _request(
    session: Session,
    context: TenantContext,
    request_id: UUID,
    action: str,
    value: Any,
    draft_id: UUID | None = None,
) -> CampaignCopyDraft | None:
    # 请求锁在父草稿锁之前，跨草稿复用同一键也不能并发改写幂等记录。
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"campaign-copy:{context.tenant_id}:{request_id}"},
    )
    prior = session.get(CampaignCopyRequest, (context.tenant_id, request_id))
    if prior is None:
        return None
    if (
        prior.action != action
        or prior.digest != digest(value)
        or (draft_id is not None and prior.draft_id != draft_id)
    ):
        raise DomainError("idempotency_conflict", "请求标识已用于其他复制操作")
    return get_draft(session, context=context, draft_id=prior.draft_id, write=True)


def _record(
    session: Session, row: CampaignCopyDraft, request_id: UUID, action: str, value: Any
) -> None:
    session.add(
        CampaignCopyRequest(
            tenant_id=row.tenant_id,
            draft_id=row.id,
            request_id=request_id,
            action=action,
            digest=digest(value),
        )
    )
    session.flush()


def create(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    selection_id: UUID,
    request_id: UUID,
) -> CampaignCopyDraft:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="build"
    )
    value = {"bc_id": bc_id, "selection_id": str(selection_id)}
    prior = _request(session, context, request_id, "create", value)
    if prior:
        return prior
    selection = get_frozen_selection(
        session, context=context, bc_id=bc_id, selection_id=selection_id
    )
    refs = list(dict.fromkeys(selection.refs))
    if (
        not refs
        or len(refs) > 100
        or selection.material_uses
        or any(ref.kind != "campaign" for ref in refs)
    ):
        raise DomainError("copy_selection_invalid", "请从系列维度选择 1–100 条来源")
    route = freeze_route(session, context=context, bc_id=bc_id)
    from app.modules.builds.copy_source import load_copy_source

    members = []
    for ref in refs:
        source = load_copy_source(
            session,
            context=context,
            bc_id=bc_id,
            advertiser_id=ref.advertiser_id,
            campaign_id=ref.remote_id,
        )
        source = json.loads(json.dumps(source, default=str))
        source.update(advertiser_id=ref.advertiser_id, campaign_id=ref.remote_id)
        members.append(
            {
                "row_id": str(uuid4()),
                "source": source,
                "source_digest": digest(source),
                "configuration": {
                    "target_advertiser_ids": [ref.advertiser_id],
                    "copies": 1,
                    "budget": None,
                    "target_roas": None,
                    "targeting": None,
                    "delivery_config": {
                        "schedule_mode": "IMMEDIATE",
                        "schedule_start_time": None,
                        "schedule_end_time": None,
                    },
                    "material_ids": [
                        str(m["material_id"]) for m in source.get("materials", [])
                    ],
                },
            }
        )
    row = CampaignCopyDraft(
        tenant_id=context.tenant_id,
        bc_id=bc_id,
        actor_id=context.actor_id,
        route=route.model_dump(mode="json"),
        rows=members,
    )
    session.add(row)
    session.flush()
    _record(session, row, request_id, "create", value)
    return row


def _revision(row: CampaignCopyDraft, expected: int) -> None:
    if row.revision != expected:
        raise DomainError("copy_revision_conflict", "复制草稿已更新，请重新加载")
    if row.status == "SUBMITTED":
        raise DomainError("copy_already_submitted", "复制草稿已提交，请新建复制批次")


def _validate_rows(
    session: Session,
    context: TenantContext,
    row: CampaignCopyDraft,
    members: list[dict[str, Any]],
    *,
    strict: bool,
) -> None:
    route = FrozenTikTokRoute.model_validate(row.route)
    count = 0
    checked: dict[str, Any] = {}
    for item in members:
        source, conf = item["source"], item["configuration"]
        resolve_account_access(
            session,
            context=context,
            bc_id=row.bc_id,
            advertiser_id=source["advertiser_id"],
            action="read",
        )
        if not strict and source.get("blockers"):
            # 阻断行允许保存其他行的修改，但不可被预览/提交偷偷忽略。
            continue
        if source.get("blockers"):
            raise DomainError(
                "copy_source_blocked",
                f"{source.get('name', '')}：来源配置不完整或当前不支持复制",
            )
        config = CopyConfiguration.model_validate(conf)
        allowed = {str(m["material_id"]) for m in source.get("materials", [])}
        if not {str(m) for m in config.material_ids} <= allowed:
            raise DomainError("copy_material_invalid", "只能选择该源系列已核验的素材")
        if (
            config.target_roas is not None
            and source.get("bid_strategy") != "TARGET_ROAS"
        ):
            raise DomainError(
                "copy_bid_strategy_invalid", "最高价值系列不能直接设置目标 ROAS"
            )
        for advertiser in config.target_advertiser_ids:
            if advertiser not in checked:
                verify_route(
                    session,
                    context=context,
                    route=route,
                    advertiser_id=advertiser,
                    capability="build",
                )
                checked[advertiser] = resolve_account_access(
                    session,
                    context=context,
                    bc_id=row.bc_id,
                    advertiser_id=advertiser,
                    connection_id=route.connection_id,
                    action="build",
                )
            if checked[advertiser].currency != source.get("currency"):
                raise DomainError(
                    "copy_currency_mismatch",
                    "目标账户币种与来源不同，请分别配置，不自动换算预算",
                )
        count += len(config.target_advertiser_ids) * config.copies
    if count > MAX_SERIES:
        raise DomainError("copy_limit_exceeded", "一次复制最多生成 1000 条新系列")


def patch(
    session: Session, *, context: TenantContext, draft_id: UUID, body: CopyPatchRequest
) -> CampaignCopyDraft:
    value = body.model_dump(mode="json", exclude={"request_id"})
    prior = _request(session, context, body.request_id, "patch", value, draft_id)
    if prior:
        return prior
    row = get_draft(session, context=context, draft_id=draft_id, write=True)
    _revision(row, body.expected_revision)
    if row.status == "PREPARING":
        raise DomainError("copy_preparing", "复制预览正在准备，请等待完成后修改")
    members = deepcopy(row.rows)
    replacements = {
        str(r.row_id): r.configuration.model_dump(mode="json") for r in body.rows
    }
    if not set(replacements) <= {r["row_id"] for r in members}:
        raise DomainError("copy_selection_invalid", "修改包含其他草稿的来源")
    for item in members:
        if item["row_id"] in replacements:
            item["configuration"] = replacements[item["row_id"]]
    _validate_rows(session, context, row, members, strict=False)
    row.rows, row.children, row.preview_digest, row.error_code = members, [], None, None
    row.revision += 1
    row.status = "DRAFT"
    session.add(row)
    _record(session, row, body.request_id, "patch", value)
    return row


def preview(
    session: Session,
    *,
    context: TenantContext,
    draft_id: UUID,
    body: CopyRevisionRequest,
) -> CampaignCopyDraft:
    value = body.model_dump(mode="json", exclude={"request_id"})
    prior = _request(session, context, body.request_id, "preview", value, draft_id)
    if prior:
        return prior
    row = get_draft(session, context=context, draft_id=draft_id, write=True)
    _revision(row, body.expected_revision)
    if row.status in {"PREPARING", "READY"}:
        _record(session, row, body.request_id, "preview", value)
        return row
    _validate_rows(session, context, row, row.rows, strict=True)
    started_at = datetime.now(UTC).isoformat()
    row.children = [
        {
            "row_id": r["row_id"],
            "advertiser_id": a,
            "ordinal": n,
            "preview_id": None,
            "submission_id": None,
            "status": "PENDING",
            "error_code": None,
            "started_at": started_at,
        }
        for r in row.rows
        for a in r["configuration"]["target_advertiser_ids"]
        for n in range(1, r["configuration"]["copies"] + 1)
    ]
    row.status, row.error_code = "PREPARING", None
    row.generation += 1
    from app.modules.campaign_copies.tasks import queue

    queue(session, row)
    _record(session, row, body.request_id, "preview", value)
    return row


def submit(
    session: Session, *, context: TenantContext, draft_id: UUID, body: CopySubmitRequest
) -> CampaignCopyDraft:
    value = body.model_dump(mode="json", exclude={"request_id"})
    prior = _request(session, context, body.request_id, "submit", value, draft_id)
    if prior:
        return prior
    row = get_draft(session, context=context, draft_id=draft_id, write=True)
    _revision(row, body.expected_revision)
    if (
        row.status != "READY"
        or not row.preview_digest
        or body.preview_digest != row.preview_digest
    ):
        raise DomainError("copy_preview_invalid", "请先生成当前修订的完整复制预览")
    if row.preview_digest != digest(
        {
            "revision": row.revision,
            "rows": row.rows,
            "children": row.children,
            "route": row.route,
        }
    ):
        raise DomainError("copy_preview_invalid", "复制预览内容与已冻结摘要不一致")
    _validate_rows(session, context, row, row.rows, strict=True)
    from app.modules.builds.copy_source import load_copy_source
    from app.modules.builds.submissions import submit_preview

    # READY 不是永久授权；提交边界重新核验来源当前事实及读取连接，
    # 已成功提交的幂等重放在上方返回，不会被后续来源变化改写。
    for item in row.rows:
        source = item["source"]
        current = load_copy_source(
            session,
            context=context,
            bc_id=row.bc_id,
            advertiser_id=source["advertiser_id"],
            campaign_id=source["campaign_id"],
        )
        if digest(current) != item["source_digest"]:
            raise DomainError(
                "copy_source_changed", "来源系列已变化，请重新选择来源生成复制草稿"
            )
    if not row.children or any(child["status"] != "READY" for child in row.children):
        raise DomainError("copy_preview_invalid", "复制子预览尚未完整准备")

    children = deepcopy(row.children)
    for child in children:
        preview_id = UUID(child["preview_id"])
        saved = session.get(BuildPreview, preview_id)
        if (
            saved is None
            or saved.tenant_id != context.tenant_id
            or saved.bc_id != row.bc_id
            or saved.status != "FROZEN"
            or saved.content_digest != child.get("content_digest")
        ):
            raise DomainError(
                "copy_preview_invalid", "子预览不存在、过期或不属于当前范围"
            )
        receipt = submit_preview(
            session,
            context=context,
            preview_id=preview_id,
            request_id=uuid5(row.id, f"submit:{row.revision}:{preview_id}"),
        )
        child["submission_id"], child["status"] = (
            str(receipt.submission_id),
            "SUBMITTED",
        )
    row.children, row.status = children, "SUBMITTED"
    session.add(row)
    _record(session, row, body.request_id, "submit", value)
    return row
