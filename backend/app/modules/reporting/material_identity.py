"""非 Spark 视频主素材用库 MID 精确补 VID，不走 TikTok 帖子身份接口。"""

from sqlalchemy import text

from app.core.errors import DomainError
from app.modules.reporting import diagnostics
from app.modules.reporting.material_value import (
    MATERIAL_WINDOW_VALUES_SQL,
    material_value_parameters,
    valuable_material_condition,
)

ASSET_BATCH = 20  # file/video/ad/search 当前 material_ids 上限；不同于 video_ids。


@diagnostics.timed("links_asset_candidates")
def pending_asset_ids(session, job):
    """尚未在本轮读取的精确 MID；也供终页判断是否仍有可立即执行的身份工作。"""
    checked = set(job.progress.get("asset_identity_checked", []))
    return tuple(
        session.connection()
        .execute(
            text(f"""
            WITH material_values AS ({MATERIAL_WINDOW_VALUES_SQL})
            SELECT DISTINCT l.main_material_id FROM material_post_link l
            JOIN material_report_day d ON d.tenant_id=l.tenant_id
              AND d.advertiser_id=l.advertiser_id AND d.main_material_id=l.main_material_id
            WHERE l.tenant_id=:t AND l.advertiser_id=:a
              AND d.stat_date BETWEEN :start AND :end
              AND {valuable_material_condition("d")}
              AND l.main_material_type='VIDEO_NON_SPARK_ADS'
              AND d.main_material_type='VIDEO_NON_SPARK_ADS'
              AND NULLIF(l.platform_material_id, '') IS NULL
              AND NOT (l.main_material_id=ANY(:checked))
            ORDER BY l.main_material_id LIMIT :limit
            """),
            {
                **material_value_parameters(
                    job.tenant_id,
                    [(job.advertiser_ids[0], job.start_date, job.end_date)],
                ),
                "t": job.tenant_id,
                "a": job.advertiser_ids[0],
                "start": job.start_date,
                "end": job.end_date,
                "checked": sorted(checked),
                "limit": ASSET_BATCH,
            },
        )
        .scalars()
    )


@diagnostics.timed("links_asset_resolve")
def resolve_asset_videos(session, job, reader) -> bool:
    """每续步至多一次精确读取；只更新已证实归属、同账户同日期的非 Spark 视频。"""
    checked = set(job.progress.get("asset_identity_checked", []))
    ids = pending_asset_ids(session, job)
    if not ids:
        return False
    page = reader.material_videos(advertiser_id=job.advertiser_ids[0], material_ids=ids)
    # 精确过滤最多20个MID，只有完整且无歧义的回执才写映射；名字不参与证明。
    pairs = {}
    if page.next_page is not None or page.total_number != len(page.rows):
        raise DomainError("report_response_invalid", "素材身份回执不完整")
    for row in page.rows:
        mid, vid = row.get("material_id"), row.get("video_id")
        if (
            mid not in ids
            or mid in pairs
            or not isinstance(vid, str)
            or not vid.strip()
        ):
            raise DomainError("report_response_invalid", "素材身份回执越界或有歧义")
        pairs[mid] = vid
    if pairs:
        session.connection().execute(
            text("""
            UPDATE material_post_link SET platform_material_id=:vid
            WHERE tenant_id=:t AND advertiser_id=:a AND main_material_id=:mid
              AND main_material_type='VIDEO_NON_SPARK_ADS'
              AND NULLIF(platform_material_id, '') IS NULL
            """),
            [
                {"t": job.tenant_id, "a": job.advertiser_ids[0], "mid": mid, "vid": vid}
                for mid, vid in pairs.items()
            ],
        )
    # 空结果仅在本轮记已检查；依赖退避到期后仍可重查，不标成同步完成。
    job.progress = job.progress | {"asset_identity_checked": sorted(checked | set(ids))}
    return True
