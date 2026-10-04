"""Bounded drama pages with server-side counts across every matching account."""

from typing import cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session as SQLAlchemySession
from sqlmodel import Session

from app.core.context import TenantContext
from app.core.pagination import Page
from app.modules.accounts.resolver import encode_cursor
from app.modules.builds.preview_schemas import (
    PreviewDramaPublic,
    bid_strategy_label,
    budget_strategy_label,
    build_structure_summary,
    frozen_bid_strategy,
)
from app.modules.builds.previews import _page_scope, _preview


def get_preview_dramas(
    session: Session,
    *,
    context: TenantContext,
    preview_id: UUID,
    cursor: str | None = None,
    limit: int = 50,
) -> Page[PreviewDramaPublic]:
    preview = _preview(session, context, preview_id)
    scope, after = _page_scope(context, preview_id, "preview_dramas", limit, cursor)
    total = int(
        cast(SQLAlchemySession, session)
        .execute(
            text(
                "SELECT count(*) FROM preview_drama "
                "WHERE tenant_id=:tenant AND preview_id=:preview"
            ),
            {"tenant": context.tenant_id, "preview": preview_id},
        )
        .scalar_one()
    )
    rows = (
        cast(SQLAlchemySession, session)
        .execute(
            text("""
      WITH page AS (
        SELECT drama_id,title FROM preview_drama WHERE tenant_id=:tenant AND preview_id=:preview
          AND (CAST(:after AS uuid) IS NULL OR drama_id > CAST(:after AS uuid)) ORDER BY drama_id LIMIT :page_size
      )
        SELECT p.drama_id,p.title,
        (SELECT count(DISTINCT s.material_id) FROM preview_skipped_material s
          JOIN build_unit b ON b.tenant_id=s.tenant_id AND b.id=s.unit_id
          WHERE b.tenant_id=:tenant AND b.preview_id=:preview AND b.drama_id=p.drama_id) AS skipped_material_count,
        (SELECT count(*) FROM preview_drama_group g WHERE g.tenant_id=:tenant AND g.preview_id=:preview AND g.drama_id=p.drama_id) AS material_group_count,
        (SELECT count(*) FROM preview_group_material m WHERE m.tenant_id=:tenant AND m.preview_id=:preview AND m.drama_id=p.drama_id) AS material_count,
        (SELECT count(DISTINCT m.material_id) FROM preview_group_material m WHERE m.tenant_id=:tenant AND m.preview_id=:preview AND m.drama_id=p.drama_id) AS unique_material_count,
        (SELECT count(*) FROM preview_ad_material m WHERE m.tenant_id=:tenant AND m.preview_id=:preview AND m.drama_id=p.drama_id) AS ad_material_allocation_count,
        u.*
      FROM page p CROSS JOIN LATERAL (
        SELECT count(*) AS account_count,
          count(*) FILTER (WHERE readiness='READY') AS ready_count,
          count(*) FILTER (WHERE readiness='PREPARING') AS preparing_count,
          count(*) FILTER (WHERE readiness='BLOCKED') AS blocked_count,
          count(*) FILTER (WHERE readiness IN ('READY','PREPARING')) AS eligible_campaign_count,
          coalesce(sum(group_count) FILTER (WHERE readiness IN ('READY','PREPARING')),0) AS eligible_adgroup_count,
          coalesce(sum(ad_count) FILTER (WHERE readiness IN ('READY','PREPARING')),0) AS eligible_ad_count
        FROM build_unit WHERE tenant_id=:tenant AND preview_id=:preview AND drama_id=p.drama_id AND complete
      ) u ORDER BY p.drama_id
    """),
            {
                "tenant": context.tenant_id,
                "preview": preview_id,
                "after": after,
                "page_size": limit + 1,
            },
        )
        .mappings()
        .all()
    )
    return Page(
        items=[
            PreviewDramaPublic(
                **{
                    **dict(row),
                    "material_allocation_count": int(row["material_count"] or 0),
                    "daily_budget_label": budget_strategy_label(
                        preview.config.get("budget_strategy")
                    ),
                    "structure_summary": build_structure_summary(
                        campaign_count=int(row["eligible_campaign_count"] or 0),
                        group_count=int(row["eligible_adgroup_count"] or 0),
                        ad_count=int(row["eligible_ad_count"] or 0),
                        creative_count=int(preview.config.get("creative_count", 1)),
                        group_generation_mode=preview.config.get(
                            "group_generation_mode", "FIXED"
                        ),
                        ad_generation_mode=preview.config.get(
                            "ad_generation_mode", "BY_MATERIAL"
                        ),
                        material_allocation_count=int(
                            row["ad_material_allocation_count"] or 0
                        ),
                        unique_material_count=int(row["unique_material_count"] or 0),
                    )
                    + f"；预算策略 {budget_strategy_label(preview.config.get('budget_strategy'))}；竞价策略 {bid_strategy_label(frozen_bid_strategy(scene_snapshot={}, preview_config=preview.config, target_roas=preview.target_roas))}",
                },
                daily_budget_sum=preview.budget
                * int(
                    row["eligible_adgroup_count"]
                    if preview.config.get("budget_strategy") == "ADGROUP"
                    else row["eligible_campaign_count"]
                ),
            )
            for row in rows[:limit]
        ],
        next_cursor=encode_cursor(scope=scope, last_id=str(rows[limit - 1]["drama_id"]))
        if len(rows) > limit
        else None,
        total=total,
    )
