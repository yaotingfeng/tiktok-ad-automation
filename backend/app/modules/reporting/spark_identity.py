"""Spark 旧帖缺广告内素材桥接时，用官方内容摘要匹配同广告的唯一视频。"""

import hashlib
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import text


def _scope(configuration):
    identities, videos = set(), set()
    for row in configuration.get("creative_list") or []:
        info = row.get("creative_info") or {}
        video = (info.get("video_info") or {}).get("video_id")
        identity = tuple(
            info.get(k)
            for k in ("identity_type", "identity_id", "identity_authorized_bc_id")
        )
        if video and identity[0] and identity[1]:
            identities.add(identity)
            videos.add(video)
    # 多身份不能猜帖子属于哪一个；超过单次官方批量上限时保留原概览路径。
    if len(identities) != 1 or not 1 <= len(videos) <= 50:
        return None
    identity = dict(
        zip(
            ("identity_type", "identity_id", "identity_authorized_bc_id"),
            next(iter(identities)),
            strict=True,
        )
    )
    return {k: v for k, v in identity.items() if v is not None}, sorted(videos)


def resolve_spark_videos(session, job, reader) -> bool:
    """每步至多一个物理请求；同广告最多缓存50个摘要，成功/缺失均不循环重查。"""
    checked = set(job.progress.get("spark_identity_checked", []))
    rows = (
        session.connection()
        .execute(
            text("""
        SELECT l.main_material_id,l.ad_remote_id FROM material_post_link l
        WHERE l.tenant_id=:t AND l.advertiser_id=:a
          AND l.main_material_type='VIDEO_SPARK_ADS'
          AND NULLIF(l.ad_material_id,'') IS NULL
          AND NULLIF(l.platform_material_id,'') IS NULL
          AND NOT (jsonb_build_array(l.main_material_id,l.ad_remote_id)::text=ANY(:checked))
          AND EXISTS (SELECT 1 FROM material_report_day d WHERE d.tenant_id=l.tenant_id
            AND d.advertiser_id=l.advertiser_id AND d.main_material_id=l.main_material_id
            AND d.main_material_type='VIDEO_SPARK_ADS' AND d.stat_date BETWEEN :start AND :end)
        ORDER BY l.ad_remote_id,l.main_material_id LIMIT 50
    """),
            {
                "t": job.tenant_id,
                "a": job.advertiser_ids[0],
                "start": job.start_date,
                "end": job.end_date,
                "checked": sorted(checked),
            },
        )
        .mappings()
        .all()
    )

    def save(**extra):
        job.progress = (
            job.progress | {"spark_identity_checked": sorted(checked)} | extra
        )

    configurations = {}
    for row in rows:
        key = json.dumps([row.main_material_id, row.ad_remote_id])
        if row.ad_remote_id not in configurations:
            # 大广告配置只取一次，不能为同一广告的50条旧帖重复搬运完整创意列表。
            configurations[row.ad_remote_id] = (
                session.connection()
                .execute(
                    text("""SELECT configuration FROM ad_object WHERE tenant_id=:t
                AND advertiser_id=:a AND kind='ad' AND remote_id=:ad"""),
                    {
                        "t": job.tenant_id,
                        "a": job.advertiser_ids[0],
                        "ad": row.ad_remote_id,
                    },
                )
                .scalar_one_or_none()
                or {}
            )
        scope = _scope(configurations[row.ad_remote_id])
        if scope is None:
            checked.add(key)
            continue
        identity, videos = scope
        scope_key = hashlib.sha256(
            json.dumps([row.ad_remote_id, identity, videos], sort_keys=True).encode()
        ).hexdigest()
        now = datetime.now(UTC)
        cache = job.progress.get("spark_asset_content", {})
        if cache.get("scope") != scope_key or datetime.fromisoformat(
            cache["at"]
        ) < now - timedelta(minutes=5):
            assets = reader.material_video_content(
                advertiser_id=job.advertiser_ids[0], video_ids=tuple(videos)
            )
            save(
                spark_asset_content={
                    "scope": scope_key,
                    "at": now.isoformat(),
                    "rows": assets,
                }
            )
            return True
        content = reader.spark_video_content(
            advertiser_id=job.advertiser_ids[0],
            item_id=row.main_material_id,
            **identity,
        )
        matches = {
            asset["video_id"]
            for asset in cache["rows"]
            if content is not None
            and asset.get("signature") == content["signature"]
            and asset.get("size") == content["size"]
        }
        if len(matches) == 1:
            # 摘要、大小与唯一候选共同证明；名字和播放URL均不参与，既有VID不覆盖。
            session.connection().execute(
                text("""
                UPDATE material_post_link SET platform_material_id=:vid
                WHERE tenant_id=:t AND advertiser_id=:a AND main_material_id=:mid
                  AND ad_remote_id=:ad AND main_material_type='VIDEO_SPARK_ADS'
                  AND NULLIF(platform_material_id,'') IS NULL
            """),
                {
                    "t": job.tenant_id,
                    "a": job.advertiser_ids[0],
                    "mid": row.main_material_id,
                    "ad": row.ad_remote_id,
                    "vid": next(iter(matches)),
                },
            )
        checked.add(key)
        save()
        return True
    save()
    return len(rows) == 50
