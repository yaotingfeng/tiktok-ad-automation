"""Spark 旧帖缺广告内素材桥接时，用官方内容摘要匹配同广告的唯一视频。"""

import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import Date, bindparam, text

from app.modules.reporting import diagnostics
from app.modules.reporting.material_value import (
    TYPED_CANDIDATE_MATERIAL_WINDOW_VALUES_SQL,
    material_value_parameters,
    valuable_material_condition,
)

# 只包含有效身份和VID；忽略名称/状态/排序，避免普通刷新反复唤醒。SQL别名a为广告对象。
IDENTITY_SIGNATURE_SQL = """(
  SELECT md5(COALESCE(jsonb_agg(DISTINCT value ORDER BY value),'[]'::jsonb)::text)
  FROM (
    SELECT jsonb_build_array(
      creative->'creative_info'->>'identity_type',
      creative->'creative_info'->>'identity_id',
      creative->'creative_info'->>'identity_authorized_bc_id',
      creative->'creative_info'->'video_info'->>'video_id') AS value
    FROM jsonb_array_elements(CASE
      WHEN jsonb_typeof(a.configuration->'creative_list')='array'
      THEN a.configuration->'creative_list' ELSE '[]'::jsonb END) creative
    WHERE NULLIF(creative->'creative_info'->>'identity_type','') IS NOT NULL
      AND NULLIF(creative->'creative_info'->>'identity_id','') IS NOT NULL
      AND NULLIF(creative->'creative_info'->'video_info'->>'video_id','') IS NOT NULL
  ) evidence
)"""


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


@diagnostics.timed("links_spark_candidates")
def pending_spark_links(session, job):
    """已有归属且本轮尚未核验内容的帖子；完成扫描不代表这些读取已经执行。"""
    checked = set(job.progress.get("spark_identity_checked", []))
    # 先固定缺桥接集合，空集合不读金额事实；LATERAL 只聚合去重素材键。
    # 金额结果必须物化一次，避免被后续 EXISTS 内联后按每个 owner 重算。
    # 本任务只有一个必填日期窗口；正净额已蕴含同租户/账户/类型的窗口内事实存在。
    # 冻结的粗日期边界由 Date 安全渲染，让 generic plan 也能正确估算分区范围；
    # 账户、素材及精确窗口数组仍绑定，不手工拼接业务参数。
    return (
        session.connection()
        .execute(
            text(f"""
        WITH missing_base AS MATERIALIZED (
          SELECT l.tenant_id,l.advertiser_id,l.main_material_id,l.main_material_type,l.ad_remote_id
          FROM material_post_link l
          WHERE l.tenant_id=:t AND l.advertiser_id=:a
            AND l.main_material_type='VIDEO_SPARK_ADS'
            AND NULLIF(l.ad_material_id,'') IS NULL
            AND NULLIF(l.platform_material_id,'') IS NULL),
        material_values AS MATERIALIZED (
          SELECT value.* FROM (
            SELECT DISTINCT advertiser_id,main_material_id,main_material_type
            FROM missing_base
          ) material_candidate CROSS JOIN LATERAL (
            {TYPED_CANDIDATE_MATERIAL_WINDOW_VALUES_SQL}
            OFFSET 0
          ) value),
        missing AS MATERIALIZED (
          SELECT l.main_material_id,l.ad_remote_id FROM missing_base l
          WHERE {valuable_material_condition("l", typed=True)}),
        scopes AS MATERIALIZED (
          SELECT owners.ad_remote_id, {IDENTITY_SIGNATURE_SQL} AS identity_signature
          FROM (SELECT DISTINCT ad_remote_id FROM missing) owners
          LEFT JOIN ad_object a ON a.tenant_id=:t AND a.advertiser_id=:a
            AND a.kind='ad' AND a.remote_id=owners.ad_remote_id)
        SELECT m.main_material_id,m.ad_remote_id,s.identity_signature
        FROM missing m JOIN scopes s ON s.ad_remote_id=m.ad_remote_id
        WHERE NOT (jsonb_build_array(m.main_material_id,m.ad_remote_id,s.identity_signature)::text=ANY(:checked))
        ORDER BY m.ad_remote_id,m.main_material_id LIMIT 50
    """).bindparams(
                bindparam("value_scan_start", type_=Date, literal_execute=True),
                bindparam("value_scan_end", type_=Date, literal_execute=True),
            ),
            {
                **material_value_parameters(
                    job.tenant_id,
                    [(job.advertiser_ids[0], job.start_date, job.end_date)],
                ),
                "t": job.tenant_id,
                "a": job.advertiser_ids[0],
                "checked": sorted(checked),
            },
        )
        .mappings()
        .all()
    )


@diagnostics.timed("links_spark_resolve")
def resolve_spark_videos(session, job, reader) -> bool:
    """每步至多一个物理请求；同广告最多缓存50个摘要，成功/缺失均不循环重查。"""
    checked = set(job.progress.get("spark_identity_checked", []))
    rows = pending_spark_links(session, job)

    def save(**extra):
        job.progress = (
            job.progress | {"spark_identity_checked": sorted(checked)} | extra
        )

    configurations = {}
    for row in rows:
        # 否定结果只适用于当时的身份/VID集合。目录晚到或候选变化必须重新核验；
        # 名称、状态、观察时间及创意排序改变不属于新的身份证据。
        key = json.dumps(
            [row.main_material_id, row.ad_remote_id, row.identity_signature]
        )
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
        # 缓存只有VID/内容摘要/大小，不含临时URL；同一身份和VID范围内作为
        # 本轮证据续用。按墙钟过期会让排队超过五分钟的每一步都重读素材，
        # 永远到不了帖子匹配。范围变化立即失效，依赖退避开启下一轮时清除。
        if cache.get("scope") != scope_key:
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
