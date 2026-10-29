"""已发布目录的轮播说明与冻结来源跳转；不参与任何金额 JOIN。"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlmodel import Session


class FrozenCarouselLink(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    submission_id: UUID
    unit_id: UUID
    group_id: UUID
    planned_ad_id: UUID


def frozen_carousel_links(
    session: Session,
    *,
    tenant_id: UUID,
    bc_id: str,
    advertiser_id: str,
    ad_id: str,
) -> tuple[FrozenCarouselLink, ...]:
    # 只链接同租户/BC/实际目标账户的成功广告；晚到或不一致回执不能证明冻结来源。
    # 一个广告只产生一个来源链接，多个回执不展开金额或重复来源。
    rows = (
        session.connection()
        .execute(
            text("""
        SELECT s.submission_id, s.unit_id, s.group_id, s.planned_ad_id
        FROM execution_step s JOIN build_unit u
          ON u.tenant_id=s.tenant_id AND u.id=s.unit_id AND u.preview_id=s.preview_id
          AND u.bc_id=s.bc_id
        JOIN planned_ad a ON a.tenant_id=s.tenant_id AND a.preview_id=s.preview_id AND a.id=s.planned_ad_id
        JOIN planned_group g ON g.tenant_id=s.tenant_id AND g.preview_id=s.preview_id AND g.id=a.group_id
          AND g.id=s.group_id AND g.unit_id=s.unit_id
        WHERE s.tenant_id=:t AND s.bc_id=:bc AND u.advertiser_id=:adv
          AND s.kind='AD' AND s.remote_id=:ad AND s.status='SUCCEEDED' AND NOT s.mismatch
          AND NOT EXISTS (SELECT 1 FROM execution_step rb
            WHERE rb.tenant_id=s.tenant_id AND rb.submission_id=s.submission_id
              AND rb.parent_step_id=s.id AND rb.kind='READBACK' AND rb.mismatch)
          AND s.group_id IS NOT NULL AND s.planned_ad_id IS NOT NULL
          AND EXISTS (SELECT 1 FROM preview_ad_creative_unit cu
            WHERE cu.tenant_id=s.tenant_id AND cu.preview_id=s.preview_id
              AND cu.drama_id=u.drama_id AND cu.group_no=g.group_no AND cu.base_ad_no=a.base_ad_no
              AND EXISTS (SELECT 1 FROM preview_creative_unit frozen
                WHERE frozen.tenant_id=cu.tenant_id AND frozen.preview_id=cu.preview_id
                  AND frozen.id=cu.creative_unit_id AND frozen.kind='CAROUSEL'))
          AND EXISTS (SELECT 1 FROM step_evidence e
            WHERE e.tenant_id=s.tenant_id AND e.submission_id=s.submission_id AND e.step_id=s.id
              AND e.attempt=s.attempt AND e.conclusion IN ('CREATED','RECONCILED') AND e.summary->>'remote_id'=s.remote_id
              AND COALESCE(e.summary->>'mismatch','false')='false')
        ORDER BY s.submission_id, s.id LIMIT 2
    """),
            {"t": tenant_id, "bc": bc_id, "adv": advertiser_id, "ad": ad_id},
        )
        .mappings()
        .all()
    )
    # 多个已确认任务声称同一远端广告时保留歧义，不任选一个跳转目标。
    return (FrozenCarouselLink(**rows[0]),) if len(rows) == 1 else ()
