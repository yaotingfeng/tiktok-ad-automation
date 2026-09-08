"""Bounded drama pages with server-side counts across every matching account."""

from typing import cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session as SQLAlchemySession
from sqlmodel import Session

from app.core.context import TenantContext
from app.core.pagination import Page
from app.modules.accounts.resolver import encode_cursor
from app.modules.builds.preview_schemas import PreviewDramaPublic
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
    rows = (
        cast(SQLAlchemySession, session)
        .execute(
            text("""
      WITH page AS (
        SELECT drama_id,title FROM preview_drama WHERE tenant_id=:tenant AND preview_id=:preview
          AND (CAST(:after AS uuid) IS NULL OR drama_id > CAST(:after AS uuid)) ORDER BY drama_id LIMIT :page_size
      )
      SELECT p.drama_id,p.title,
        (SELECT count(*) FROM preview_drama_group g WHERE g.tenant_id=:tenant AND g.preview_id=:preview AND g.drama_id=p.drama_id) AS material_group_count,
        (SELECT count(*) FROM preview_group_material m WHERE m.tenant_id=:tenant AND m.preview_id=:preview AND m.drama_id=p.drama_id) AS material_count,
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
                **row,
                daily_budget_sum=preview.budget * int(row["eligible_campaign_count"]),
            )
            for row in rows[:limit]
        ],
        next_cursor=encode_cursor(scope=scope, last_id=str(rows[limit - 1]["drama_id"]))
        if len(rows) > limit
        else None,
    )
