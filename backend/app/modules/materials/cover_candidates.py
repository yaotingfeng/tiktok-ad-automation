"""封面共享候选查询；规划与领取使用同一搭建窗口。"""

from sqlalchemy import or_
from sqlmodel import col, select

from app.modules.builds.execution_window import cover_admission_condition

from .cover_models import MaterialCoverJob


def candidate_query(first: MaterialCoverJob):
    conditions = (
        MaterialCoverJob.tenant_id == first.tenant_id,
        MaterialCoverJob.bc_id == first.bc_id,
        MaterialCoverJob.actor_id == first.actor_id,
        MaterialCoverJob.connection_id == first.connection_id,
        MaterialCoverJob.frozen_route == first.frozen_route,
        MaterialCoverJob.purpose == "BUILD",
        MaterialCoverJob.status == "PENDING",
        cover_admission_condition(),
        col(MaterialCoverJob.error_code).is_distinct_from("cover_window_wait"),
        col(MaterialCoverJob.share_batch_id).is_(None),
        col(MaterialCoverJob.request_armed_at).is_(None),
        col(MaterialCoverJob.known_image_id).is_(None),
    )
    # 同一候选集合只计算一次；三个内联副本会放大规划/JIT成本，
    # 实际查询尚未读取业务数据就已超过短事务期限。
    eligible = (
        select(
            MaterialCoverJob.id,
            MaterialCoverJob.material_id,
            MaterialCoverJob.advertiser_id,
        )
        .where(*conditions)
        .cte("eligible_cover_candidates")
        .prefix_with("MATERIALIZED", dialect="postgresql")
    )
    material_ids = (
        select(eligible.c.material_id)
        .where(eligible.c.material_id != first.material_id)
        .distinct()
        .order_by(eligible.c.material_id)
        .limit(19)
    )
    advertisers = (
        select(eligible.c.advertiser_id)
        .where(eligible.c.advertiser_id != first.advertiser_id)
        .distinct()
        .order_by(eligible.c.advertiser_id)
        .limit(9)
    )
    return (
        select(MaterialCoverJob)
        .where(col(MaterialCoverJob.id).in_(select(eligible.c.id)))
        .where(
            or_(
                col(MaterialCoverJob.material_id) == first.material_id,
                col(MaterialCoverJob.material_id).in_(material_ids),
            ),
            or_(
                col(MaterialCoverJob.advertiser_id) == first.advertiser_id,
                col(MaterialCoverJob.advertiser_id).in_(advertisers),
            ),
        )
        .order_by(
            col(MaterialCoverJob.material_id), col(MaterialCoverJob.advertiser_id)
        )
        .limit(200)
    )
