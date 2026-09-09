import hashlib
import json
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime
from random import Random
from time import monotonic
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy import and_, func
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.core.pagination import Page
from app.modules.accounts.access import resolve_account_access
from app.modules.accounts.resolver import decode_cursor, encode_cursor
from app.modules.builds.drafts import get_draft
from app.modules.builds.models import (
    DraftAccount,
    DraftDrama,
    DraftGroupMaterial,
    DraftInput,
)
from app.modules.builds.preview_models import (
    BuildPreview,
    BuildUnit,
    PlannedAd,
    PlannedGroup,
    PreviewCopy,
    PreviewDrama,
    PreviewDramaGroup,
    PreviewGroupMaterial,
    PreviewInput,
)
from app.modules.builds.preview_schemas import (
    FrozenAd,
    FrozenGroup,
    FrozenUnit,
    PreviewInputPublic,
    PreviewSummary,
    PreviewUnit,
    Readiness,
)
from app.modules.builds.preview_validation import measured, name_reasons, scene_reasons
from app.modules.builds.scene import read_scene_context
from app.modules.builds.scene_schemas import SceneContext
from app.modules.materials.readiness import get_material_readiness_batch
from app.modules.providers.models import PromotionLink
from app.modules.strategies.naming import render_names
from app.modules.strategies.schemas import StrategyConfig
from app.modules.strategies.service import get_copies, get_version
from app.modules.tenants.permissions import require_tenant

"""Preview planning is local and never dispatches target assets or ads."""


def iter_pairs[D, A](
    dramas: Iterable[D], accounts: Callable[[], Iterable[A]]
) -> Iterator[tuple[D, A]]:
    for drama in dramas:
        for account in accounts():
            yield drama, account


def unit_readiness(
    currency: str,
    account_currency: str,
    material_states: Iterable[str],
    scene_reasons: Iterable[str],
) -> Literal["READY", "PREPARING", "BLOCKED"]:
    states = tuple(material_states)
    if (
        currency != account_currency
        or tuple(scene_reasons)
        or not states
        or any(state not in {"ready", "preparable"} for state in states)
    ):
        return "BLOCKED"
    return "PREPARING" if "preparable" in states else "READY"


# Local engineering transaction/page sizes, never TikTok account limits.
PAGE_SIZE = 100
STEP_LIMIT = 200


def _authorize(
    session: Session, context: TenantContext, *, write: bool = False
) -> None:
    require_tenant(
        session,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        action="build" if write else "read",
    )


def _preview(
    session: Session, context: TenantContext, identity: UUID, *, lock: bool = False
) -> BuildPreview:
    _authorize(session, context, write=lock)
    statement = select(BuildPreview).where(
        BuildPreview.tenant_id == context.tenant_id, BuildPreview.id == identity
    )
    if lock:
        statement = statement.with_for_update()
    row = session.exec(
        statement.execution_options(populate_existing=True)
    ).one_or_none()
    if row is None:
        raise DomainError("preview_not_found", "预览不存在")
    return row


def _hash(previous: str, value: Any) -> str:
    return hashlib.sha256(
        (
            previous
            + json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                default=str,
            )
        ).encode()
    ).hexdigest()


def _record(preview: BuildPreview, value: Any) -> None:
    preview.progress["digest"] = _hash(preview.progress.get("digest", ""), value)


def generate_preview(
    session: Session, *, context: TenantContext, draft_id: UUID, expected_revision: int
) -> UUID:
    draft = get_draft(session, context=context, draft_id=draft_id, lock=True)
    if draft.revision != expected_revision:
        raise DomainError("draft_revision_conflict", "草稿已变更")
    existing = session.exec(
        select(BuildPreview).where(
            BuildPreview.tenant_id == context.tenant_id,
            BuildPreview.draft_id == draft_id,
            BuildPreview.draft_revision == expected_revision,
        )
    ).one_or_none()
    if existing:
        return existing.id
    if draft.status != "READY":
        raise DomainError("draft_not_ready", "草稿尚未准备完成")
    config = get_version(session, context=context, version_id=draft.strategy_version_id)
    row = BuildPreview(
        tenant_id=context.tenant_id,
        bc_id=draft.bc_id,
        draft_id=draft_id,
        draft_revision=draft.revision,
        strategy_version_id=draft.strategy_version_id,
        actor_id=context.actor_id,
        batch_short_id="",
        local_date=datetime.now(UTC).strftime("%Y%m%d"),
        config=config.model_dump(mode="json"),
        budget=config.budget,
        target_roas=config.target_roas,
    )
    # The full random UUID is a unique batch discriminator, never a short prefix
    # that can collide under large-account workloads.
    row.batch_short_id = row.id.hex
    row.progress = {
        "phase": "inputs",
        "kind": "drama",
        "after": 0,
        "digest": _hash(
            "",
            {
                "preview_id": row.id,
                "draft_revision": draft.revision,
                "config": row.config,
                "date": row.local_date,
                "batch": row.batch_short_id,
            },
        ),
    }
    session.add(row)
    session.flush()
    from app.modules.builds.preview_tasks import queue_preview

    queue_preview(session, row)
    session.flush()
    return row.id


def _scope(preview: BuildPreview) -> dict[str, Any]:
    return {
        "tenant_id": preview.tenant_id,
        "preview_id": preview.id,
        "bc_id": preview.bc_id,
    }


def _snapshot_inputs(session: Session, preview: BuildPreview) -> None:
    p = preview.progress
    rows = session.exec(
        select(DraftInput)
        .where(
            DraftInput.tenant_id == preview.tenant_id,
            DraftInput.draft_id == preview.draft_id,
            DraftInput.kind == p["kind"],
            DraftInput.line_no > p["after"],
        )
        .order_by(col(DraftInput.line_no))
        .limit(PAGE_SIZE)
    ).all()
    for row in rows:
        value = PreviewInput(
            **_scope(preview),
            kind=row.kind,
            line_no=row.line_no,
            raw_text=row.raw_text,
            status=row.status,
            reason_code=row.reason_code,
            duplicate_of=row.duplicate_of,
        )
        session.add(value)
        _record(preview, value.model_dump(mode="json"))
    if rows:
        p["after"] = rows[-1].line_no
    else:
        p["after"] = 0
        if p["kind"] == "drama":
            p["kind"] = "account"
        else:
            p.update(
                phase="dramas",
                drama_after=None,
                current_drama=None,
                group_after=0,
                position=0,
            )


def _snapshot_drama(
    session: Session,
    context: TenantContext,
    preview: BuildPreview,
    config: StrategyConfig,
) -> None:
    p = preview.progress
    if not p["current_drama"]:
        query = select(DraftDrama).where(
            DraftDrama.tenant_id == preview.tenant_id,
            DraftDrama.draft_id == preview.draft_id,
        )
        if p["drama_after"]:
            query = query.where(DraftDrama.drama_id > UUID(p["drama_after"]))
        drama = session.exec(query.order_by(col(DraftDrama.drama_id)).limit(1)).first()
        if drama is None:
            p.update(
                phase="units",
                drama_after=None,
                current_drama=None,
                account_after="",
                unit_id=None,
                group_after=0,
            )
            return
        link = session.exec(
            select(PromotionLink).where(
                PromotionLink.tenant_id == preview.tenant_id,
                PromotionLink.id == drama.link_id,
            )
        ).one()
        frozen = PreviewDrama(
            **_scope(preview),
            drama_id=drama.drama_id,
            link_id=drama.link_id,
            title=drama.title,
            url=link.url or "",
            protected_base=link.protected_base or "",
            reason_codes=[]
            if link.status == "ready" and link.url
            else ["link_unavailable"],
        )
        session.add(frozen)
        session.flush()
        _record(preview, frozen.model_dump(mode="json"))
        p.update(
            current_drama=str(drama.drama_id),
            group_after=0,
            position=0,
            current_group=None,
        )
        return
    drama_id = UUID(p["current_drama"])
    if p["current_group"] is None:
        number = session.exec(
            select(DraftGroupMaterial.group_no)
            .where(
                DraftGroupMaterial.tenant_id == preview.tenant_id,
                DraftGroupMaterial.draft_id == preview.draft_id,
                DraftGroupMaterial.drama_id == drama_id,
                DraftGroupMaterial.group_no > p["group_after"],
            )
            .order_by(col(DraftGroupMaterial.group_no))
            .limit(1)
        ).first()
        if number is None:
            p.update(drama_after=p["current_drama"], current_drama=None)
            return
        group = PreviewDramaGroup(**_scope(preview), drama_id=drama_id, group_no=number)
        session.add(group)
        session.flush()
        _record(preview, group.model_dump(mode="json"))
        pool = get_copies(session, context=context, version_id=config.copy_pool_version)
        # Sampling occurs only while creating the shared group, before account expansion.
        seed = int(_hash("", [preview.id, drama_id, number]), 16)
        choices = Random(seed).sample(list(pool), config.creative_count)
        for n, copy in enumerate(choices, 1):
            value = PreviewCopy(
                **_scope(preview),
                drama_id=drama_id,
                group_no=number,
                creative_no=n,
                copy_id=copy.copy_id,
                text=copy.text,
            )
            session.add(value)
            _record(preview, value.model_dump(mode="json"))
        p.update(current_group=number, position=0)
        return
    rows = session.exec(
        select(DraftGroupMaterial)
        .where(
            DraftGroupMaterial.tenant_id == preview.tenant_id,
            DraftGroupMaterial.draft_id == preview.draft_id,
            DraftGroupMaterial.drama_id == drama_id,
            DraftGroupMaterial.group_no == p["current_group"],
            DraftGroupMaterial.position > p["position"],
        )
        .order_by(col(DraftGroupMaterial.position))
        .limit(PAGE_SIZE)
    ).all()
    for row in rows:
        material_row = PreviewGroupMaterial(
            **_scope(preview),
            drama_id=drama_id,
            group_no=row.group_no,
            position=row.position,
            material_id=row.material_id,
        )
        session.add(material_row)
        _record(preview, material_row.model_dump(mode="json"))
    if rows:
        p["position"] = rows[-1].position
    else:
        p.update(group_after=p["current_group"], current_group=None, position=0)


def _names(
    preview: BuildPreview,
    drama: PreviewDrama,
    config: StrategyConfig,
    group: int,
    creative: int,
) -> tuple[str, str, str]:
    # The engineering ceiling prevents malformed local text allocation; official
    # per-level measurements are checked separately, including CJK weighting.
    return render_names(
        protected_base=drama.protected_base,
        title=drama.title,
        date_text=preview.local_date,
        batch_short_id=preview.batch_short_id,
        suffix=config.campaign_suffix,
        group_no=group,
        creative_no=creative,
        max_length=10000,
    )


def _block(unit: BuildUnit, reasons: Iterable[str]) -> None:
    unit.reason_codes = list(dict.fromkeys([*unit.reason_codes, *reasons]))
    if unit.reason_codes:
        unit.readiness = "BLOCKED"


def _expand_unit(
    session: Session,
    context: TenantContext,
    preview: BuildPreview,
    config: StrategyConfig,
) -> None:
    p = preview.progress
    if not p["current_drama"]:
        query = select(PreviewDrama).where(
            PreviewDrama.tenant_id == preview.tenant_id,
            PreviewDrama.preview_id == preview.id,
        )
        if p["drama_after"]:
            query = query.where(PreviewDrama.drama_id > UUID(p["drama_after"]))
        drama = session.exec(
            query.order_by(col(PreviewDrama.drama_id)).limit(1)
        ).first()
        if drama is None:
            p.update(phase="digest", after_id=None)
            return
        p.update(
            current_drama=str(drama.drama_id),
            account_after="",
            unit_id=None,
            group_after=0,
        )
        return
    drama = session.get(
        PreviewDrama, (preview.tenant_id, preview.id, UUID(p["current_drama"]))
    )
    assert drama
    if not p["unit_id"]:
        account = session.exec(
            select(DraftAccount)
            .where(
                DraftAccount.tenant_id == preview.tenant_id,
                DraftAccount.draft_id == preview.draft_id,
                DraftAccount.advertiser_id > p["account_after"],
            )
            .order_by(col(DraftAccount.advertiser_id))
            .limit(1)
        ).first()
        if account is None:
            p.update(drama_after=p["current_drama"], current_drama=None)
            return
        try:
            current_access = resolve_account_access(
                session,
                context=context,
                bc_id=preview.bc_id,
                advertiser_id=account.advertiser_id,
                action="read",
            )
            if current_access.connection_id != account.connection_id:
                raise DomainError(
                    "account_authorization_changed", "账户授权已改变，请重新准备草稿"
                )
            if (
                current_access.currency != account.currency
                or current_access.timezone != account.timezone
            ):
                raise DomainError(
                    "account_metadata_changed", "账户信息已改变，请重新准备草稿"
                )
            scene = read_scene_context(
                session,
                context=context,
                bc_id=preview.bc_id,
                advertiser_id=account.advertiser_id,
                link_id=drama.link_id,
            )
        except DomainError as error:
            if error.code not in {
                "account_not_in_bc",
                "account_ownership_conflict",
                "account_metadata_incomplete",
                "account_access_denied",
                "account_authorization_changed",
                "account_metadata_changed",
                "scene_link_unavailable",
                "resource_not_found",
            }:
                raise
            scene = SceneContext(
                supported=False,
                reason_codes=(error.code,),
                capability_revision="unavailable",
            )
        name = _names(preview, drama, config, 1, 1)[0]
        unit = BuildUnit(
            **_scope(preview),
            drama_id=drama.drama_id,
            advertiser_id=account.advertiser_id,
            connection_id=account.connection_id,
            currency=account.currency,
            timezone=account.timezone,
            campaign_name=name,
            campaign_digest=hashlib.sha256(name.encode()).hexdigest(),
            scene_snapshot=scene.to_snapshot(),
        )
        _block(
            unit,
            [
                *drama.reason_codes,
                *scene_reasons(config, scene, account.currency),
                *name_reasons(name, "campaign", unit.scene_snapshot),
            ],
        )
        # A protected provider base may collide across distinct dramas. Retain
        # every combination, explicitly excluding all colliding campaigns.
        collisions = session.exec(
            select(BuildUnit).where(
                BuildUnit.tenant_id == preview.tenant_id,
                BuildUnit.preview_id == preview.id,
                BuildUnit.advertiser_id == account.advertiser_id,
                BuildUnit.campaign_digest == unit.campaign_digest,
                BuildUnit.campaign_name == name,
            )
        ).all()
        if collisions:
            _block(unit, ["duplicate_campaign_name"])
            for old in collisions:
                _block(old, ["duplicate_campaign_name"])
                session.add(old)
        session.add(unit)
        session.flush()
        p.update(
            unit_id=str(unit.id), group_after=0, account_after=account.advertiser_id
        )
        return
    resumed_unit = session.get(BuildUnit, UUID(p["unit_id"]))
    assert resumed_unit
    unit = resumed_unit
    group = session.exec(
        select(PreviewDramaGroup)
        .where(
            PreviewDramaGroup.tenant_id == preview.tenant_id,
            PreviewDramaGroup.preview_id == preview.id,
            PreviewDramaGroup.drama_id == drama.drama_id,
            PreviewDramaGroup.group_no > p["group_after"],
        )
        .order_by(col(PreviewDramaGroup.group_no))
        .limit(1)
    ).first()
    if group is None:
        if unit.group_count == 0:
            _block(unit, ["materials_missing"])
        unit.complete = True
        session.add(unit)
        p.update(unit_id=None, group_after=0)
        return
    count = session.exec(
        select(func.count())
        .select_from(PreviewGroupMaterial)
        .where(
            PreviewGroupMaterial.tenant_id == preview.tenant_id,
            PreviewGroupMaterial.preview_id == preview.id,
            PreviewGroupMaterial.drama_id == drama.drama_id,
            PreviewGroupMaterial.group_no == group.group_no,
        )
    ).one()
    maximum = unit.scene_snapshot["creative_limit"]
    if not maximum or count > maximum:
        _block(
            unit,
            ["material_group_limit_exceeded" if maximum else "field_limits_unverified"],
        )
    else:
        # Only scene-validated bounded groups reach asset readiness checks.
        materials = session.exec(
            select(PreviewGroupMaterial.material_id)
            .where(
                PreviewGroupMaterial.tenant_id == preview.tenant_id,
                PreviewGroupMaterial.preview_id == preview.id,
                PreviewGroupMaterial.drama_id == drama.drama_id,
                PreviewGroupMaterial.group_no == group.group_no,
            )
            .order_by(col(PreviewGroupMaterial.position))
        ).all()
        readiness = (
            get_material_readiness_batch(
                session,
                context=context,
                bc_id=preview.bc_id,
                material_ids=list(materials),
                advertiser_id=unit.advertiser_id,
            )
            if materials
            else {}
        )
        for result in readiness.values():
            if result.state == "blocked":
                _block(unit, [result.reason_code or "material_unavailable"])
            elif result.state == "preparable" and unit.readiness != "BLOCKED":
                unit.readiness = "PREPARING"
    names = _names(preview, drama, config, group.group_no, 1)
    planned = PlannedGroup(
        **_scope(preview),
        unit_id=unit.id,
        drama_id=drama.drama_id,
        group_no=group.group_no,
        name=names[1],
    )
    _block(unit, name_reasons(planned.name, "adgroup", unit.scene_snapshot))
    session.add(planned)
    session.flush()
    _record(preview, planned.model_dump(mode="json"))
    copies = session.exec(
        select(PreviewCopy)
        .where(
            PreviewCopy.tenant_id == preview.tenant_id,
            PreviewCopy.preview_id == preview.id,
            PreviewCopy.drama_id == drama.drama_id,
            PreviewCopy.group_no == group.group_no,
        )
        .order_by(col(PreviewCopy.creative_no))
    ).all()
    for copy in copies:
        ad_name = _names(preview, drama, config, group.group_no, copy.creative_no)[2]
        _block(unit, name_reasons(ad_name, "ad", unit.scene_snapshot))
        method = unit.scene_snapshot["field_constraints"].get("copy_measurement")
        copy_limit = unit.scene_snapshot["copy_length_limit"]
        if not copy.text.strip():
            _block(unit, ["copy_empty"])
        elif method not in {"characters", "cjk_weighted"} or not copy_limit:
            _block(unit, ["field_limits_unverified"])
        elif measured(copy.text, method) > copy_limit:
            _block(unit, ["copy_too_long"])
        options = list(config.cta_option_ids) or list(
            unit.scene_snapshot["cta_fields"].get("asset_ids", [])
        )
        ad = PlannedAd(
            **_scope(preview),
            group_id=planned.id,
            creative_no=copy.creative_no,
            name=ad_name,
            copy_id=copy.copy_id,
            text=copy.text,
            cta_option_ids=options,
        )
        session.add(ad)
        _record(preview, ad.model_dump(mode="json"))
    unit.group_count += 1
    unit.ad_count += len(copies)
    session.add(unit)
    p["group_after"] = group.group_no


def _digest_units(session: Session, preview: BuildPreview) -> bool:
    p = preview.progress
    query = select(BuildUnit).where(
        BuildUnit.tenant_id == preview.tenant_id, BuildUnit.preview_id == preview.id
    )
    if p["after_id"]:
        query = query.where(BuildUnit.id > UUID(p["after_id"]))
    rows = session.exec(query.order_by(col(BuildUnit.id)).limit(PAGE_SIZE)).all()
    for row in rows:
        _record(preview, row.model_dump(mode="json"))
    if rows:
        p["after_id"] = str(rows[-1].id)
        return False
    preview.content_digest = p["digest"]
    preview.status = "FROZEN"
    return True


def continue_preview(
    session: Session,
    *,
    context: TenantContext,
    preview_id: UUID,
    step_limit: int = STEP_LIMIT,
) -> bool:
    if type(step_limit) is not int or not 1 <= step_limit <= STEP_LIMIT:
        raise ValueError("Invalid preview step count")
    initial = _preview(session, context, preview_id)
    # Same parent-first order as editing and the revision invalidation trigger.
    draft = get_draft(session, context=context, draft_id=initial.draft_id, lock=True)
    preview = _preview(session, context, preview_id, lock=True)
    if preview.status != "BUILDING":
        return True
    if draft.revision != preview.draft_revision:
        preview.status = "OBSOLETE"
        session.add(preview)
        session.flush()
        return True
    config = StrategyConfig.model_validate(preview.config)
    # SQLAlchemy JSON mutations are tracked by replacing the container once.
    preview.progress = dict(preview.progress)
    deadline = monotonic() + 20
    for _ in range(step_limit):
        phase = preview.progress["phase"]
        if phase == "inputs":
            _snapshot_inputs(session, preview)
        elif phase == "dramas":
            _snapshot_drama(session, context, preview, config)
        elif phase == "units":
            _expand_unit(session, context, preview, config)
        elif _digest_units(session, preview):
            break
        session.flush()
        if monotonic() >= deadline:
            break
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(preview, "progress")
    session.add(preview)
    session.flush()
    return preview.status != "BUILDING"


def get_preview_summary(
    session: Session, *, context: TenantContext, preview_id: UUID
) -> PreviewSummary:
    preview = _preview(session, context, preview_id)
    rows = session.exec(
        select(
            BuildUnit.readiness,
            func.count(),
            func.sum(BuildUnit.group_count),
            func.sum(BuildUnit.ad_count),
        )
        .where(
            BuildUnit.tenant_id == context.tenant_id,
            BuildUnit.preview_id == preview_id,
            col(BuildUnit.complete).is_(True),
        )
        .group_by(col(BuildUnit.readiness))
    ).all()  # noqa: E712
    counts = {state: (int(n), int(g or 0), int(a or 0)) for state, n, g, a in rows}
    eligible = [counts.get(state, (0, 0, 0)) for state in ("READY", "PREPARING")]
    n, g, a = (sum(x[i] for x in eligible) for i in range(3))
    issue_count = session.exec(
        select(func.count())
        .select_from(PreviewInput)
        .where(
            PreviewInput.tenant_id == context.tenant_id,
            PreviewInput.preview_id == preview_id,
            col(PreviewInput.status).not_in(["matched", "ready"]),
        )
    ).one()
    return PreviewSummary(
        preview_id=preview.id,
        draft_id=preview.draft_id,
        draft_revision=preview.draft_revision,
        bc_id=preview.bc_id,
        status=cast(
            Literal["BUILDING", "FROZEN", "OBSOLETE", "FAILED"], preview.status
        ),
        currency=preview.config["currency"],
        campaign_count=n,
        adgroup_count=g,
        ad_count=a,
        blocked_count=counts.get("BLOCKED", (0, 0, 0))[0],
        preparing_count=counts.get("PREPARING", (0, 0, 0))[0],
        input_issue_count=issue_count,
        total_unit_count=sum(x[0] for x in counts.values()),
        daily_budget_sum=preview.budget * n,
        content_digest=preview.content_digest,
        error_code=preview.error_code,
        created_at=preview.created_at,
    )


def _page_scope(
    context: TenantContext,
    identity: UUID,
    kind: str,
    limit: int,
    cursor: str | None,
    **extra: Any,
) -> tuple[dict[str, Any], str | None]:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise DomainError("configuration_invalid", "分页大小无效")
    scope = {
        "kind": kind,
        "tenant_id": str(context.tenant_id),
        "id": str(identity),
        **extra,
    }
    return scope, decode_cursor(cursor, scope=scope)


def get_preview_units(
    session: Session,
    *,
    context: TenantContext,
    preview_id: UUID,
    cursor: str | None = None,
    limit: int = 50,
    readiness: str | None = None,
    drama_id: UUID | None = None,
) -> Page[PreviewUnit]:
    preview = _preview(session, context, preview_id)
    scope, after = _page_scope(
        context,
        preview_id,
        "preview_units",
        limit,
        cursor,
        readiness=readiness,
        drama_id=str(drama_id) if drama_id else None,
    )
    query = (
        select(BuildUnit, PreviewDrama)
        .join(
            PreviewDrama,
            and_(
                col(PreviewDrama.tenant_id) == BuildUnit.tenant_id,
                col(PreviewDrama.preview_id) == BuildUnit.preview_id,
                col(PreviewDrama.drama_id) == BuildUnit.drama_id,
            ),
        )
        .where(
            BuildUnit.tenant_id == context.tenant_id,
            BuildUnit.preview_id == preview_id,
            col(BuildUnit.complete).is_(True),
        )
    )  # noqa: E712
    if readiness:
        query = query.where(BuildUnit.readiness == readiness)
    if drama_id:
        query = query.where(BuildUnit.drama_id == drama_id)
    if after:
        query = query.where(BuildUnit.id > UUID(after))
    rows = session.exec(query.order_by(col(BuildUnit.id)).limit(limit + 1)).all()
    return Page(
        items=[
            PreviewUnit(
                unit_id=u.id,
                drama_id=u.drama_id,
                title=d.title,
                advertiser_id=u.advertiser_id,
                currency=u.currency,
                budget=preview.budget,
                campaign_name=u.campaign_name,
                readiness=cast(Readiness, u.readiness),
                reason_codes=u.reason_codes,
                group_count=u.group_count,
                ad_count=u.ad_count,
            )
            for u, d in rows[:limit]
        ],
        next_cursor=encode_cursor(scope=scope, last_id=str(rows[limit - 1][0].id))
        if len(rows) > limit
        else None,
    )


def _unit(
    session: Session, context: TenantContext, unit_id: UUID
) -> tuple[BuildPreview, BuildUnit, PreviewDrama]:
    _authorize(session, context)
    unit = session.exec(
        select(BuildUnit).where(
            BuildUnit.tenant_id == context.tenant_id, BuildUnit.id == unit_id
        )
    ).one_or_none()
    if unit is None:
        raise DomainError("preview_not_found", "预览单元不存在")
    preview = _preview(session, context, unit.preview_id)
    if preview.content_digest is None or not unit.complete:
        raise DomainError("preview_not_frozen", "预览尚未完成")
    drama = session.get(PreviewDrama, (context.tenant_id, preview.id, unit.drama_id))
    assert drama
    return preview, unit, drama


def load_frozen_unit(
    session: Session, *, context: TenantContext, unit_id: UUID
) -> FrozenUnit:
    preview, unit, drama = _unit(session, context, unit_id)
    return FrozenUnit(
        unit_id=unit.id,
        preview_id=preview.id,
        tenant_id=context.tenant_id,
        drama_id=drama.drama_id,
        link_id=drama.link_id,
        strategy_version_id=preview.strategy_version_id,
        bc_id=preview.bc_id,
        advertiser_id=unit.advertiser_id,
        connection_id=unit.connection_id,
        currency=unit.currency,
        timezone=unit.timezone,
        campaign_name=unit.campaign_name,
        protected_base=drama.protected_base,
        url=drama.url,
        budget=preview.budget,
        target_roas=preview.target_roas,
        readiness=cast(Readiness, unit.readiness),
        reason_codes=tuple(unit.reason_codes),
        scene_snapshot=unit.scene_snapshot,
    )


def get_frozen_groups(
    session: Session,
    *,
    context: TenantContext,
    unit_id: UUID,
    cursor: str | None = None,
    limit: int = 20,
) -> Page[FrozenGroup]:
    preview, unit, _ = _unit(session, context, unit_id)
    scope, after = _page_scope(context, unit_id, "frozen_groups", limit, cursor)
    query = select(PlannedGroup).where(
        PlannedGroup.tenant_id == context.tenant_id, PlannedGroup.unit_id == unit_id
    )
    if after:
        query = query.where(PlannedGroup.group_no > int(after))
    rows = session.exec(
        query.order_by(col(PlannedGroup.group_no)).limit(limit + 1)
    ).all()
    items = []
    for group in rows[:limit]:
        materials = session.exec(
            select(PreviewGroupMaterial.material_id)
            .where(
                PreviewGroupMaterial.tenant_id == context.tenant_id,
                PreviewGroupMaterial.preview_id == preview.id,
                PreviewGroupMaterial.drama_id == unit.drama_id,
                PreviewGroupMaterial.group_no == group.group_no,
            )
            .order_by(col(PreviewGroupMaterial.position))
            .limit(101)
        ).all()
        if len(materials) > 100:
            raise DomainError(
                "preview_group_too_large", "素材分组超过可用上限，请先调整草稿分组"
            )
        ads = session.exec(
            select(PlannedAd)
            .where(
                PlannedAd.tenant_id == context.tenant_id, PlannedAd.group_id == group.id
            )
            .order_by(col(PlannedAd.creative_no))
            .limit(101)
        ).all()
        if len(ads) > 100:
            raise DomainError("preview_group_too_large", "创意数量超过可用上限")
        items.append(
            FrozenGroup(
                group_id=group.id,
                group_no=group.group_no,
                name=group.name,
                material_ids=tuple(materials),
                ads=tuple(
                    FrozenAd(
                        ad_id=a.id,
                        creative_no=a.creative_no,
                        name=a.name,
                        copy_id=a.copy_id,
                        text=a.text,
                        cta_option_ids=tuple(a.cta_option_ids),
                    )
                    for a in ads
                ),
            )
        )
    return Page(
        items=items,
        next_cursor=encode_cursor(scope=scope, last_id=str(rows[limit - 1].group_no))
        if len(rows) > limit
        else None,
    )


def get_preview_inputs(
    session: Session,
    *,
    context: TenantContext,
    preview_id: UUID,
    kind: str,
    cursor: str | None = None,
    limit: int = 50,
    issues_only: bool = False,
    status: str | None = None,
) -> Page[PreviewInputPublic]:
    _preview(session, context, preview_id)
    if kind not in {"drama", "account"}:
        raise DomainError("draft_input_invalid", "输入类别无效")
    scope, after = _page_scope(
        context,
        preview_id,
        "preview_inputs",
        limit,
        cursor,
        input_kind=kind,
        issues_only=str(issues_only),
        status=status,
    )
    query = select(PreviewInput).where(
        PreviewInput.tenant_id == context.tenant_id,
        PreviewInput.preview_id == preview_id,
        PreviewInput.kind == kind,
    )
    if issues_only:
        query = query.where(col(PreviewInput.status).not_in(["matched", "ready"]))
    if status:
        query = query.where(PreviewInput.status == status)
    if after:
        query = query.where(PreviewInput.line_no > int(after))
    rows = session.exec(
        query.order_by(col(PreviewInput.line_no)).limit(limit + 1)
    ).all()
    return Page(
        items=[PreviewInputPublic(**r.model_dump()) for r in rows[:limit]],
        next_cursor=encode_cursor(scope=scope, last_id=str(rows[limit - 1].line_no))
        if len(rows) > limit
        else None,
    )
