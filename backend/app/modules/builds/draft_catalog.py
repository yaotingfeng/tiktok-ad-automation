"""未提交搭建目录：仅查询本地持久记录，不启动准备或平台请求。"""

from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session as SASession
from sqlmodel import Session

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.core.pagination import Page
from app.modules.accounts.models import TenantBC
from app.modules.accounts.resolver import decode_cursor, encode_cursor
from app.modules.builds.schemas import DraftListItem
from app.modules.tenants.permissions import require_tenant


def list_drafts(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    cursor: str | None = None,
    limit: int = 50,
) -> Page[DraftListItem]:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    if session.get(TenantBC, (context.tenant_id, bc_id)) is None:
        raise DomainError("account_not_in_bc", "当前租户 BC 不存在")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise DomainError("invalid_page_size", "每页最多 100 条")
    scope = {"tenant": str(context.tenant_id), "bc": bc_id, "kind": "unfinished-drafts"}
    after = decode_cursor(cursor, scope=scope)
    at, identity = None, None
    if after:
        try:
            stamp, value = after.split("|")
            at, identity = datetime.fromisoformat(stamp), UUID(value)
            if at.tzinfo is None:
                raise ValueError
        except ValueError, TypeError:
            raise DomainError("invalid_cursor", "分页游标无效") from None
    total = int(
        cast(SASession, session).execute(
            text("""
SELECT count(*) FROM build_draft d
WHERE d.tenant_id=:tenant AND d.bc_id=:bc
AND NOT EXISTS (SELECT 1 FROM build_submission s
  WHERE s.tenant_id=d.tenant_id AND s.draft_id=d.id)
"""),
            {"tenant": context.tenant_id, "bc": bc_id},
        ).scalar_one()
    )
    # 已提交批次统一从搭建任务继续；先分页，再统计该页输入，避免展开剧目×账户。
    rows = (
        cast(SASession, session)
        .execute(
            text("""
WITH page AS MATERIALIZED (
 SELECT d.* FROM build_draft d
 WHERE d.tenant_id=:tenant AND d.bc_id=:bc
 AND NOT EXISTS (SELECT 1 FROM build_submission s
   WHERE s.tenant_id=d.tenant_id AND s.draft_id=d.id)
 AND (CAST(:at AS timestamptz) IS NULL OR (d.updated_at,d.id)<(:at,CAST(:identity AS uuid)))
 ORDER BY d.updated_at DESC,d.id DESC LIMIT :size
)
SELECT d.id draft_id,d.bc_id,d.revision,d.status,d.updated_at,
 st.name || ' v' || sv.number::text strategy_label,
 ARRAY(SELECT i.raw_text FROM draft_input i
   WHERE i.tenant_id=d.tenant_id AND i.draft_id=d.id AND i.kind='drama'
   AND i.status<>'empty' AND i.duplicate_of IS NULL
   ORDER BY i.line_no LIMIT 3) drama_titles,
 inputs.drama_input_count,inputs.account_input_count,
 (SELECT count(*) FROM draft_account a
   WHERE a.tenant_id=d.tenant_id AND a.draft_id=d.id) resolved_account_count,
 p.id preview_id,p.status preview_status
FROM page d
JOIN strategy_version sv ON sv.tenant_id=d.tenant_id AND sv.id=d.strategy_version_id
JOIN strategy st ON st.tenant_id=sv.tenant_id AND st.id=sv.strategy_id
CROSS JOIN LATERAL (
 SELECT count(*) FILTER (WHERE i.kind='drama') drama_input_count,
        count(*) FILTER (WHERE i.kind='account') account_input_count
 FROM draft_input i WHERE i.tenant_id=d.tenant_id AND i.draft_id=d.id AND i.status<>'empty'
) inputs
LEFT JOIN build_preview p ON p.tenant_id=d.tenant_id AND p.draft_id=d.id AND p.draft_revision=d.revision
ORDER BY d.updated_at DESC,d.id DESC
"""),
            {
                "tenant": context.tenant_id,
                "bc": bc_id,
                "at": at,
                "identity": identity,
                "size": limit + 1,
            },
        )
        .mappings()
        .all()
    )
    items = [DraftListItem.model_validate(row) for row in rows[:limit]]
    return Page(
        items=items,
        next_cursor=encode_cursor(
            scope=scope,
            last_id=f"{items[-1].updated_at.isoformat()}|{items[-1].draft_id}",
        )
        if len(rows) > limit
        else None,
        total=total,
    )
