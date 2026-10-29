"""素材日报主素材 → 广告 → 素材库视频的对照维护。

素材日报（拆分接口）有收入、能按天，但不带广告；Spark 广告的主素材 ID 是每条广告
各自发布的帖子 ID。创意概览接口能按“广告 × 主素材”返回同一 ID 及广告内素材 ID，
两者配合才能把每天的素材数据归到广告、再归到视频。

实测平台行为（2026-10-14，新骏伯生产账户）：
- 概览按帖子 ID 筛选不生效；按已确认广告 ID 可筛选当前素材，日期参数不能限定帖子。
- 广告内素材被重建时平台换发新帖子和新广告内素材 ID，概览不再列出旧帖子，但旧帖子
  已有的消耗仍在素材日报里；拆分接口按广告筛选时仍能查到旧帖子。
- 广告目录可能晚于平台更新，旧帖子与新帖子的广告内素材 ID 都可能对不上目录。

因此对照里直接保存精确身份链证明的视频 ID，文件名仅用于优先查找候选广告，不能
证明历史视频身份。概览查不到的旧帖子再按同日期广告事实查拆分报表，完整查询在
任务内复用；确认归属后保留结果，缺少视频身份证据时继续保持真实缺口。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlmodel import Session

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.report_reader import RawReportPage
from app.modules.reporting.link_queries import QueryMembershipReader
from app.modules.reporting.material_value import (
    MATERIAL_WINDOW_VALUES_SQL,
    material_value_parameters,
    material_window_values,
    valuable_material_condition,
)
from app.modules.reporting.spark_identity import IDENTITY_SIGNATURE_SQL
from app.modules.reporting.store_models import MaterialLinkScan

# 主素材（帖子）→ 视频；同一主素材对应多条广告（非 Spark 共用）时取最小视频 ID，
# 保证每笔金额只归入一个视频。依赖参数 :tenant、:advs。
POST_VIDEO_SQL = """
    SELECT DISTINCT ON (l.advertiser_id, l.main_material_id, l.main_material_type)
        l.advertiser_id AS adv, l.main_material_id AS mid, l.main_material_type AS mtype, l.platform_material_id AS vid
    FROM material_post_link l
    WHERE l.tenant_id = :tenant AND l.advertiser_id = ANY(:advs)
      AND l.platform_material_id IS NOT NULL
      AND l.main_material_type IN ('VIDEO_SPARK_ADS','VIDEO_NON_SPARK_ADS')
    ORDER BY l.advertiser_id, l.main_material_id, l.main_material_type, l.platform_material_id COLLATE "C"
"""


def material_row_key(adv: str, mid: str, vid: str | None) -> str:
    """素材维度行键：对上视频的按视频跨账户一行；对不上的帖子单独一行，金额不丢。"""
    return json.dumps(["material", vid] if vid else ["post", adv, mid])


def post_video_map(
    session: Session,
    *,
    tenant_id: UUID,
    advertisers: Sequence[str],
    start: date,
    end: date,
) -> dict[tuple[str, str, str], str]:
    """所选日期有素材日报事实的帖子 → 视频（取法同 POST_VIDEO_SQL）。

    没有事实的帖子不贡献金额，也不计入快照的行账户（row_set fk 只取有事实的帖子）。
    逐条按主键查对照，不整租户读取：大租户帖子对照有数百万条。
    """
    rows = session.connection().execute(
        text(
            """
            SELECT f.adv, f.mid, f.mtype, v.vid
            FROM (
                SELECT DISTINCT advertiser_id AS adv, main_material_id AS mid, main_material_type AS mtype
                FROM material_report_day
                WHERE tenant_id = :tenant AND advertiser_id = ANY(:advs)
                  AND stat_date BETWEEN :start AND :end
            ) f
            CROSS JOIN LATERAL (
                SELECT l.platform_material_id AS vid FROM material_post_link l
                WHERE l.tenant_id = :tenant AND l.advertiser_id = f.adv
                  AND l.main_material_id = f.mid AND l.main_material_type = f.mtype
                  AND l.platform_material_id IS NOT NULL
                  AND l.main_material_type IN ('VIDEO_SPARK_ADS','VIDEO_NON_SPARK_ADS')
                ORDER BY l.platform_material_id COLLATE "C" LIMIT 1
            ) v
            """
        ),
        {"tenant": tenant_id, "advs": sorted(advertisers), "start": start, "end": end},
    )
    return {(r.adv, r.mid, r.mtype): r.vid for r in rows}


def needs_material_links(
    session: Session,
    *,
    tenant_id: UUID,
    advertiser_id: str,
    start: date,
    end: date,
) -> bool:
    """零金额帖子不影响付费指标，不因它们补链或阻断规则；真实映射保持不变。"""
    return bool(
        missing_video_ids(
            session,
            tenant_id=tenant_id,
            advertiser_id=advertiser_id,
            start=start,
            end=end,
        )
    )


# 扫描结束与映射完整是两件事；依赖不齐保留一条等待工作，不能完成后每轮重造。
DEPENDENCY_ERRORS = {"material_directory_pending", "material_identity_unavailable"}
DEPENDENCY_RECHECK = timedelta(minutes=10)
DEPENDENCY_MAX_RECHECK = timedelta(hours=6)
NEGATIVE_SCAN_TTL = timedelta(hours=6)


def missing_video_ids(
    session: Session,
    *,
    tenant_id: UUID,
    advertiser_id: str,
    start: date,
    end: date,
) -> set[str]:
    return set(
        session.connection()
        .execute(
            text(f"""
        WITH material_values AS ({MATERIAL_WINDOW_VALUES_SQL})
        SELECT v.main_material_id FROM material_values v
        WHERE v.has_value AND NOT EXISTS (
            SELECT 1 FROM material_post_link l WHERE l.tenant_id=:value_tenant
              AND l.advertiser_id=v.advertiser_id AND l.main_material_id=v.main_material_id
              AND NULLIF(l.platform_material_id, '') IS NOT NULL)
        """),
            material_value_parameters(tenant_id, [(advertiser_id, start, end)]),
        )
        .scalars()
    )


def _missing_spark_bridges(session, tenant_id, advertiser_id, *, start, end):
    """缺广告内素材 ID 的已定位 Spark；布尔值表示目录已提供完整素材证据。"""
    rows = session.connection().execute(
        text(f"""
        WITH material_values AS ({MATERIAL_WINDOW_VALUES_SQL})
        SELECT l.main_material_id,
          bool_or(a.remote_id IS NOT NULL AND EXISTS (
            SELECT 1 FROM ad_material_reference u
            WHERE u.tenant_id=l.tenant_id AND u.advertiser_id=l.advertiser_id
              AND u.ad_remote_id=l.ad_remote_id AND u.complete)) AS directory_complete
        FROM material_report_day d
        JOIN material_post_link l ON l.tenant_id=d.tenant_id
          AND l.advertiser_id=d.advertiser_id AND l.main_material_id=d.main_material_id
        LEFT JOIN ad_object a ON a.tenant_id=l.tenant_id AND a.advertiser_id=l.advertiser_id
          AND a.kind='ad' AND a.remote_id=l.ad_remote_id
        WHERE d.tenant_id=:t AND d.advertiser_id=:a
          AND (CAST(:start AS date) IS NULL OR d.stat_date>=:start)
          AND (CAST(:end AS date) IS NULL OR d.stat_date<=:end)
          AND {valuable_material_condition("d")}
          AND l.main_material_type='VIDEO_SPARK_ADS'
          AND NULLIF(l.ad_material_id, '') IS NULL
          AND NOT EXISTS (SELECT 1 FROM material_post_link done
            WHERE done.tenant_id=d.tenant_id AND done.advertiser_id=d.advertiser_id
              AND done.main_material_id=d.main_material_id
              AND NULLIF(done.platform_material_id, '') IS NOT NULL)
        GROUP BY l.main_material_id
        """),
        {
            **material_value_parameters(tenant_id, [(advertiser_id, start, end)]),
            "t": tenant_id,
            "a": advertiser_id,
            "start": start,
            "end": end,
        },
    )
    return {row.main_material_id: row.directory_complete for row in rows}


def dependency_changed(session, job):
    """只以新缺口、已补齐或新目录证据唤醒，周期触发本身不打断退避。"""
    missing = missing_video_ids(
        session,
        tenant_id=job.tenant_id,
        advertiser_id=job.advertiser_ids[0],
        start=job.start_date,
        end=job.end_date,
    )
    if missing != set(job.progress.get("waiting_ids", [])):
        return True
    return directory_signature(session, job) != job.progress.get("dependency_signature")


def directory_signature(session, job):
    # 目录热刷新会更新 observed_at，但相同素材身份不属于新证据，不能因此反复唤醒。
    return (
        session.connection()
        .execute(
            text(f"""
        WITH material_values AS ({MATERIAL_WINDOW_VALUES_SQL}), missing AS MATERIALIZED (
          SELECT DISTINCT d.main_material_id, d.main_material_name, d.stat_date
          FROM material_report_day d
          WHERE d.tenant_id=:t AND d.advertiser_id=:a AND d.stat_date BETWEEN :start AND :end
            AND {valuable_material_condition("d")}
            AND NOT EXISTS (SELECT 1 FROM material_post_link l
              WHERE l.tenant_id=d.tenant_id AND l.advertiser_id=d.advertiser_id
                AND l.main_material_id=d.main_material_id
                AND NULLIF(l.platform_material_id, '') IS NOT NULL)),
        candidates AS (
          SELECT u.ad_remote_id, u.ad_material_id, u.platform_material_id, u.name
          FROM (SELECT DISTINCT l.ad_remote_id FROM material_post_link l
                JOIN missing m ON m.main_material_id=l.main_material_id
                WHERE l.tenant_id=:t AND l.advertiser_id=:a) ads
          JOIN ad_material_reference u ON u.tenant_id=:t AND u.advertiser_id=:a
            AND u.ad_remote_id=ads.ad_remote_id AND u.complete
          UNION
          SELECT u.ad_remote_id, u.ad_material_id, u.platform_material_id, u.name
          FROM material_post_link l JOIN missing m ON m.main_material_id=l.main_material_id
          JOIN ad_material_reference u ON u.tenant_id=l.tenant_id AND u.advertiser_id=l.advertiser_id
            AND u.ad_remote_id=l.ad_remote_id AND u.ad_material_id=l.ad_material_id
          WHERE l.tenant_id=:t AND l.advertiser_id=:a AND l.main_material_type='VIDEO_SPARK_ADS'
            AND NULLIF(l.ad_material_id, '') IS NOT NULL AND u.material_type='VIDEO' AND NOT u.complete
            AND NULLIF(u.platform_material_id, '') IS NOT NULL
          UNION
          SELECT u.ad_remote_id, u.ad_material_id, u.platform_material_id, u.name
          FROM (SELECT DISTINCT m.main_material_name FROM missing m
                WHERE m.main_material_name IS NOT NULL AND NOT EXISTS (
                  SELECT 1 FROM material_post_link l WHERE l.tenant_id=:t
                    AND l.advertiser_id=:a AND l.main_material_id=m.main_material_id)) names
          JOIN ad_material_reference u ON u.tenant_id=:t AND u.advertiser_id=:a
            AND u.complete AND md5(u.name)=md5(names.main_material_name)
            AND u.name=names.main_material_name),
        evidence AS (
          SELECT jsonb_build_array(ad_remote_id, ad_material_id, platform_material_id, name)::text AS value
          FROM candidates
          UNION
          SELECT jsonb_build_array('spark_identity', owners.ad_remote_id,
            {IDENTITY_SIGNATURE_SQL})::text
          FROM (SELECT DISTINCT l.ad_remote_id FROM material_post_link l
                JOIN missing m ON m.main_material_id=l.main_material_id
                WHERE l.tenant_id=:t AND l.advertiser_id=:a
                  AND l.main_material_type='VIDEO_SPARK_ADS') owners
          LEFT JOIN ad_object a ON a.tenant_id=:t AND a.advertiser_id=:a
            AND a.kind='ad' AND a.remote_id=owners.ad_remote_id
          UNION
          SELECT jsonb_build_array('report_post', main_material_id, main_material_name, stat_date)::text
          FROM missing
          UNION
          SELECT jsonb_build_array('report_ad', ad_id, stat_date)::text FROM ad_report_day
          WHERE tenant_id=:t AND advertiser_id=:a AND stat_date BETWEEN :start AND :end
            AND campaign_automation_type='UPGRADED_SMART_PLUS')
        SELECT md5(COALESCE(string_agg(value, ',' ORDER BY value), '')) FROM evidence
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
            },
        )
        .scalar_one()
    )


def wait_for_dependencies(session, job, *, now):
    missing = missing_video_ids(
        session,
        tenant_id=job.tenant_id,
        advertiser_id=job.advertiser_ids[0],
        start=job.start_date,
        end=job.end_date,
    )
    if not missing:
        return False
    unlinked = _unlinked(
        session,
        job.tenant_id,
        job.advertiser_ids[0],
        start=job.start_date,
        end=job.end_date,
    )
    bridges = _missing_spark_bridges(
        session,
        job.tenant_id,
        job.advertiser_ids[0],
        start=job.start_date,
        end=job.end_date,
    )
    # 已有完整目录却缺帖子桥接时，继续读目录不能补出广告内素材 ID。
    # 保留真实身份缺口与等待任务；缺广告本体/完整目录的仍是可执行的目录依赖。
    job.error_code = (
        "material_identity_unavailable"
        if unlinked or any(bridges.values())
        else "material_directory_pending"
    )
    from app.modules.reporting.directory_refresh import request_link_directory

    request_link_directory(session, job, now=now)
    signature = directory_signature(session, job)
    unchanged = (
        set(job.progress.get("waiting_ids", [])) == missing
        and job.progress.get("dependency_signature") == signature
    )
    attempts = (
        min(6, int(job.progress.get("dependency_attempts", 0)) + 1) if unchanged else 0
    )
    job.due_at = now + min(DEPENDENCY_MAX_RECHECK, DEPENDENCY_RECHECK * (2**attempts))
    # 退避不能跨过已有负证据的主动复核期限（累计退避可能超过六小时）。
    scan = session.get(
        MaterialLinkScan,
        (
            job.tenant_id,
            job.advertiser_ids[0],
            _scan_scope(
                job.start_date, job.end_date, job.progress.get("overview_ad_ids", ())
            ),
        ),
    )
    if unlinked and scan is not None and scan.scanned_at + NEGATIVE_SCAN_TTL > now:
        job.due_at = min(job.due_at, scan.scanned_at + NEGATIVE_SCAN_TTL)
    if unlinked and (
        job.progress.get("scan_evidence_valid") is False
        or scan is not None
        and (
            scan.evidence_signature is None
            or scan.scanned_at + NEGATIVE_SCAN_TTL <= now
        )
    ):
        # 新证据到达、原证据已过期或旧续点没有观察起点时立即复核。
        # 不能因本轮刚结束而把旧负证据重新延长到下一轮依赖退避。
        job.due_at = now
    # 到期重读时重新确定查询范围；此前验证的映射已经持久化，不回滚。
    job.progress = {
        key: value
        for key, value in job.progress.items()
        if key
        in {
            "coalesced_v1",
            "requested_at",
            "initial_window",
            "dependency_recovery_sequences",
        }
    } | {
        "waiting_ids": sorted(missing),
        "dependency_checked_at": now.isoformat(),
        "dependency_signature": signature,
        "dependency_attempts": attempts,
    }
    return True


def _unlinked(
    session: Session,
    tenant_id: UUID,
    advertiser_id: str,
    *,
    start: date | None = None,
    end: date | None = None,
) -> dict[str, tuple[str | None, str, date, date]]:
    """尚无对照的主素材 → (最新名称, 类型, 最早日, 最晚日)。"""
    rows = session.connection().execute(
        text(f"""
        WITH material_values AS ({MATERIAL_WINDOW_VALUES_SQL})
        SELECT d.main_material_id AS mid,
            (array_agg(d.main_material_name ORDER BY d.stat_date DESC)
                FILTER (WHERE d.main_material_name IS NOT NULL))[1] AS name,
            min(d.main_material_type) AS mtype, min(d.stat_date) AS first_day,
            max(d.stat_date) AS last_day
        FROM material_report_day d
        WHERE d.tenant_id = :t AND d.advertiser_id = :a
          AND (CAST(:start AS date) IS NULL OR d.stat_date >= :start)
          AND (CAST(:end AS date) IS NULL OR d.stat_date <= :end)
          AND {valuable_material_condition("d")}
          AND NOT EXISTS (
            SELECT 1 FROM material_post_link l
            WHERE l.tenant_id = d.tenant_id AND l.advertiser_id = d.advertiser_id
              AND l.main_material_id = d.main_material_id)
        GROUP BY d.main_material_id
        ORDER BY sum(d.spend) DESC, d.main_material_id
        """),
        {
            **material_value_parameters(tenant_id, [(advertiser_id, start, end)]),
            "t": tenant_id,
            "a": advertiser_id,
            "start": start,
            "end": end,
        },
    )
    return {r.mid: (r.name, r.mtype, r.first_day, r.last_day) for r in rows}


def _link(
    tenant_id: UUID,
    advertiser_id: str,
    *,
    material_id: str,
    ad_id: str,
    material_type: str,
    ad_material_id: str | None,
    name: str | None,
    now: datetime,
) -> dict[str, Any]:
    return {
        "t": tenant_id,
        "a": advertiser_id,
        "m": material_id,
        "ad": ad_id,
        "mt": material_type,
        "am": ad_material_id,
        "name": name,
        "now": now,
    }


def _overview_link(
    row: dict[str, Any], tenant_id: UUID, advertiser_id: str, now: datetime
) -> dict[str, Any]:
    dims, metrics = row.get("dimensions") or {}, row.get("metrics") or {}
    ad_id, material_id = dims.get("smart_plus_ad_id"), dims.get("main_material_id")
    material_type = dims.get("main_material_type")
    if not ad_id or not material_id or not material_type:
        raise DomainError("report_row_invalid", "创意概览行缺少广告或主素材身份")
    return _link(
        tenant_id,
        advertiser_id,
        material_id=str(material_id),
        ad_id=str(ad_id),
        material_type=str(material_type),
        ad_material_id=str(metrics.get("ad_material_id") or "") or None,
        name=metrics.get("main_material_name") or None,
        now=now,
    )


def _upsert_links(session: Session, values: list[dict[str, Any]]) -> None:
    if not values:
        return
    # 帖子发布后不会换广告：同键只刷新广告内素材 ID、名称与观察时间，已解析的视频保留。
    session.connection().execute(
        text("""
        INSERT INTO material_post_link (tenant_id, advertiser_id, main_material_id,
            ad_remote_id, main_material_type, ad_material_id, material_name, observed_at)
        VALUES (:t, :a, :m, :ad, :mt, :am, :name, :now)
        ON CONFLICT (tenant_id, advertiser_id, main_material_id, ad_remote_id)
        DO UPDATE SET main_material_type = EXCLUDED.main_material_type,
            ad_material_id = COALESCE(EXCLUDED.ad_material_id, material_post_link.ad_material_id),
            material_name = COALESCE(EXCLUDED.material_name, material_post_link.material_name),
            observed_at = EXCLUDED.observed_at
        """),
        values,
    )


def _candidate_ads(
    session: Session,
    tenant_id: UUID,
    advertiser_id: str,
    names: set[str],
    first: date,
    last: date,
) -> tuple[list[str], int]:
    """同名目录优先，但旧素材删除/改名后仍须核查同日期有报表事实的广告。"""
    rows = session.connection().execute(
        text("""
        WITH candidates AS (
          SELECT ad_remote_id, 0 AS priority FROM ad_material_reference
          WHERE tenant_id = :t AND advertiser_id = :a AND complete
            AND md5(name) = ANY(:name_hashes) AND name = ANY(:names)
          UNION ALL
          SELECT ad_id, 1 FROM ad_report_day
          WHERE tenant_id=:t AND advertiser_id=:a AND stat_date BETWEEN :first AND :last
            AND campaign_automation_type='UPGRADED_SMART_PLUS')
        SELECT ad_remote_id, min(priority) AS priority FROM candidates GROUP BY ad_remote_id
        ORDER BY min(priority), ad_remote_id
        """),
        {
            "t": tenant_id,
            "a": advertiser_id,
            "names": sorted(names),
            "first": first,
            "last": last,
            "name_hashes": [
                hashlib.md5(name.encode()).hexdigest() for name in sorted(names)
            ],
        },
    )
    candidates = list(rows)
    return [r[0] for r in candidates], sum(r[1] == 0 for r in candidates)


# 平台按广告筛选一次最多 200 个广告。
AD_FILTER_LIMIT = 200


def _locate_post_step(state, *, read_ad_posts, first, last):
    """单帖二分路径与当前查询页持久化；每步最多一次平台读取。

    右半可继续缩小候选，但最终单广告必须正向读取。栈仅保留尚未定位的路径，避免把每次完整
    二分查找塞进一个期限；跨 200 广告批次、分页也分别提交后再继续。
    """
    stack = state["search_stack"]
    if not stack:
        offset = state["next_batch"]
        # 同名候选独立查完后再扩展报表候选，避免把原本一次命中的查询放大为整批二分。
        boundary = state.get("preferred_count", len(state["candidates"]))
        stop = (
            min(offset + AD_FILTER_LIMIT, boundary)
            if offset < boundary
            else offset + AD_FILTER_LIMIT
        )
        batch = state["candidates"][offset:stop]
        if not batch:
            return True, None
        stack.append({"ads": batch, "confirmed": False})
        state["next_batch"] += len(batch)
    node = stack[-1]
    ads = node["ads"]
    query_ads = ads[: len(ads) // 2] if node["confirmed"] and len(ads) > 1 else ads
    query = state.get("query", {"page": 1, "found": False})
    result = read_ad_posts(tuple(query_ads), first, last, query["page"])
    if result.total_number >= 20_000:
        if len(ads) == 1:
            raise DomainError(
                "report_truncation_risk", "报表行数达到平台上限，可能被截断"
            )
        # 大候选集合可能触及平台结果上限；拆为独立未确认子集，不能据截断页推导缺失。
        state.pop("query", None)
        stack.pop()
        middle = len(ads) // 2
        stack.extend(
            [
                {"ads": ads[middle:], "confirmed": False},
                {"ads": ads[:middle], "confirmed": False},
            ]
        )
        return False, None
    signature = [result.total_page, result.total_number]
    if query.get("signature", signature) != signature:
        # 翻页期间集合改变时只重读当前子集；不能把不完整的缺失推导为右半命中。
        raise DomainError("report_snapshot_changed", "定位查询分页总数变化")
    query["signature"] = signature
    query["found"] = query["found"] or any(
        str((row.get("dimensions") or {}).get("main_material_id") or "") == state["mid"]
        for row in result.rows
    )
    if result.next_page is not None:
        query["page"] = result.next_page
        state["query"] = query
        return False, None
    state.pop("query", None)
    if len(ads) == 1:
        if query["found"]:
            stack.pop()
            return True, ads[0]
        if node["confirmed"]:
            # 父集合命中过，两个子集却都没有：这是快照矛盾，不能签发六小时否定。
            raise DomainError("report_snapshot_changed", "定位查询的父子集合不一致")
        stack.pop()
        return not stack and state["next_batch"] >= len(state["candidates"]), None
    if node["confirmed"]:
        node["ads"] = query_ads if query["found"] else ads[len(ads) // 2 :]
    elif query["found"]:
        node["confirmed"] = True
    else:
        stack.pop()
        return not stack and state["next_batch"] >= len(state["candidates"]), None
    if len(node["ads"]) == 1 and query["found"] and len(query_ads) == 1:
        return True, node["ads"][0]
    return False, None


def _resolve_videos(
    session: Session,
    tenant_id: UUID,
    advertiser_id: str,
    *,
    start: date | None = None,
    end: date | None = None,
) -> None:
    """只按同广告内精确素材 ID 解析；同名文件允许异内容，不能证明历史 VID。"""
    # complete表示整页素材/创意补读覆盖，并不否定主响应已证实的精确视频身份。
    # Spark视频可用同账户、同广告、同广告内素材ID且唯一的VID直接对照；
    # 不完整的同名候选、帖子命名空间和多VID歧义均不能借此猜测，也不改广告操作的完整性要求。
    session.connection().execute(
        text(f"""
        WITH material_values AS ({MATERIAL_WINDOW_VALUES_SQL})
        UPDATE material_post_link target SET platform_material_id = resolved.vid
        FROM (
          SELECT l.main_material_id, l.ad_remote_id, min(u.platform_material_id) AS vid
          FROM material_post_link l JOIN ad_material_reference u
            ON u.tenant_id=l.tenant_id AND u.advertiser_id=l.advertiser_id
              AND u.ad_remote_id=l.ad_remote_id AND u.ad_material_id=l.ad_material_id
          WHERE l.tenant_id=:t AND l.advertiser_id=:a AND l.platform_material_id IS NULL
            AND {valuable_material_condition("l")}
            AND NULLIF(l.ad_material_id, '') IS NOT NULL
            AND NULLIF(u.platform_material_id, '') IS NOT NULL
            AND l.main_material_type IN ('VIDEO_SPARK_ADS','VIDEO_NON_SPARK_ADS')
            AND u.material_type='VIDEO'
            AND (u.complete OR (l.main_material_type='VIDEO_SPARK_ADS' AND u.material_type='VIDEO'))
          GROUP BY l.main_material_id,l.ad_remote_id
          HAVING count(DISTINCT u.platform_material_id)=1
        ) resolved
        WHERE target.tenant_id=:t AND target.advertiser_id=:a
          AND target.main_material_id=resolved.main_material_id
          AND target.ad_remote_id=resolved.ad_remote_id AND target.platform_material_id IS NULL
        """),
        {
            **material_value_parameters(tenant_id, [(advertiser_id, start, end)]),
            "t": tenant_id,
            "a": advertiser_id,
        },
    )


def _scan_scope(start=None, end=None, overview_ad_ids=()):
    if start is None and end is None and not overview_ad_ids:
        return "unscoped"
    return hashlib.md5(
        json.dumps([str(start), str(end), sorted(overview_ad_ids)]).encode()
    ).hexdigest()


def _scan_signature(
    session, tenant_id, advertiser_id, ids, start=None, end=None, overview_ad_ids=()
):
    """真实候选身份摘要；普通目录观察时间变化不是新证据。"""
    return (
        session.connection()
        .execute(
            text("""
        WITH names AS (
          SELECT DISTINCT main_material_name AS name FROM material_report_day
          WHERE tenant_id=:t AND advertiser_id=:a AND main_material_id=ANY(:ids)
            AND main_material_name IS NOT NULL), evidence AS (
          SELECT DISTINCT jsonb_build_array(u.ad_remote_id,u.ad_material_id,
              u.platform_material_id,u.name)::text AS value
          FROM names n JOIN ad_material_reference u ON u.tenant_id=:t AND u.advertiser_id=:a
            AND u.complete AND md5(u.name)=md5(n.name) AND u.name=n.name
          UNION
          SELECT jsonb_build_array('report_ad', ad_id, stat_date)::text FROM ad_report_day
          WHERE tenant_id=:t AND advertiser_id=:a
            AND (CAST(:start AS date) IS NULL OR stat_date>=:start)
            AND (CAST(:end AS date) IS NULL OR stat_date<=:end)
            AND campaign_automation_type='UPGRADED_SMART_PLUS'
          UNION
          SELECT jsonb_build_array('report_post', main_material_id, main_material_name, stat_date)::text
          FROM material_report_day WHERE tenant_id=:t AND advertiser_id=:a
            AND main_material_id=ANY(:ids)
            AND (CAST(:start AS date) IS NULL OR stat_date>=:start)
            AND (CAST(:end AS date) IS NULL OR stat_date<=:end)
          UNION SELECT CAST(:query_scope AS text))
        SELECT md5(COALESCE(string_agg(value, ',' ORDER BY value), '')) FROM evidence
    """),
            {
                "t": tenant_id,
                "a": advertiser_id,
                "ids": sorted(ids),
                "start": start,
                "end": end,
                "query_scope": json.dumps(
                    [str(start), str(end), sorted(overview_ad_ids)]
                ),
            },
        )
        .scalar_one()
    )


def _scan_evidence_started_at(
    progress: dict[str, Any], now: datetime
) -> datetime | None:
    try:
        started_at = datetime.fromisoformat(progress["scan_evidence_at"])
    except KeyError, TypeError, ValueError:
        return None
    return started_at if started_at.tzinfo is not None and started_at <= now else None


def refresh_material_links(
    session: Session,
    *,
    tenant_id: UUID,
    advertiser_id: str,
    read_overview: Callable[[int], RawReportPage],
    read_ad_posts: Callable[[tuple[str, ...], date, date, int], RawReportPage],
    now: datetime,
    start: date | None = None,
    end: date | None = None,
    progress: dict[str, Any] | None = None,
    overview_ad_ids: tuple[str, ...] = (),
) -> bool:
    """推进有界关联扫描；返回是否完成，调用方把游标、已解析关联一起提交。

    当前日报只修其日期内的帖子，旧帖子由补数扫描处理；概览逐页保存，不能因一个
    历史帖子的定位超时反复重读整账户。缺少目录的帖子保留真实缺口，不能填零。
    """
    progress = progress if progress is not None else {}
    unlinked = _unlinked(session, tenant_id, advertiser_id, start=start, end=end)
    # 本轮概览已完整时不再查询桥接依赖，避免每个旧帖二分续步重复扫描事实。
    bridges = (
        _missing_spark_bridges(session, tenant_id, advertiser_id, start=start, end=end)
        if not progress.get("overview_complete")
        else {}
    )
    scope = _scan_scope(start, end, overview_ad_ids)
    scan = session.get(MaterialLinkScan, (tenant_id, advertiser_id, scope))
    cached = (
        scan is not None
        and set(unlinked) <= set(scan.unresolved_ids)
        and scan.scanned_at > now - NEGATIVE_SCAN_TTL
    )
    if cached and unlinked:
        cached = scan.evidence_signature == _scan_signature(
            session,
            tenant_id,
            advertiser_id,
            scan.unresolved_ids,
            start,
            end,
            overview_ad_ids,
        )
    if unlinked and not cached:
        if "scan_evidence_ids" not in progress:
            # 0038在途任务的旧否定结果未绑定证据范围，不能直接签为0039的新负缓存。
            # 已持久映射和概览分页仍有效；只重做尚未找到归属的旧候选搜索。
            progress.pop("checked_ids", None)
            progress.pop("locate_post", None)
            progress["scan_evidence_ids"] = sorted(unlinked)
            progress["scan_evidence_signature"] = _scan_signature(
                session, tenant_id, advertiser_id, unlinked, start, end, overview_ad_ids
            )
            progress["scan_evidence_at"] = now.isoformat()
    # owner 已知不代表帖子桥接已知；每轮等待到期可重新读一次概览补迟到 ID。
    # 沿用冻结范围和单页断点；无 owner 的负缓存仍只约束旧帖子定位。
    if (
        progress.get("owner_lookup_first")
        and not unlinked
        and bridges
        and not any(bridges.values())
    ):
        # 归属已证明但广告目录尚未到达：等待已有目录依赖即可。全账户概览不能
        # 替代目录提供的精确视频身份；目录到达后原唤醒链会重试内容匹配。
        return True
    owner_first = (
        progress.get("owner_lookup_first")
        and not progress.get("owner_lookup_finished")
        and unlinked
        and not cached
    )
    if (
        ((unlinked and not cached) or bridges)
        and not progress.get("overview_complete")
        and not owner_first
    ):
        values = material_window_values(
            session, tenant_id, [(advertiser_id, start, end)]
        )
        page = read_overview(int(progress.get("next_page", 1)))
        _upsert_links(
            session,
            [
                _overview_link(row, tenant_id, advertiser_id, now)
                for row in page.rows
                if values.get(
                    (
                        advertiser_id,
                        str((row.get("dimensions") or {}).get("main_material_id")),
                    ),
                    False,
                )
            ],
        )
        if page.next_page is None:
            progress["overview_complete"] = True
        else:
            progress["next_page"] = page.next_page
        # 包括概览终页都先独立落库，后面的旧帖定位异常不能撤回这些页。
        _resolve_videos(session, tenant_id, advertiser_id, start=start, end=end)
        session.flush()
        return bool(progress.get("overview_complete")) and (
            cached
            or not _unlinked(session, tenant_id, advertiser_id, start=start, end=end)
        )
    if unlinked and not cached:
        stale = _unlinked(session, tenant_id, advertiser_id, start=start, end=end)
        checked = set(progress.get("checked_ids", []))
        # 有消耗的缺口先定位，零消耗历史继续保留；在途二分游标不因排序改变而丢失。
        remaining = [mid for mid in stale if mid not in checked]
        if remaining:
            current = progress.get("locate_post")
            mid = (
                current["mid"]
                if current and current["mid"] in remaining
                else remaining[0]
            )
            name, mtype, first, last = stale[mid]
            if not current or current["mid"] != mid:
                candidates, preferred_count = _candidate_ads(
                    session,
                    tenant_id,
                    advertiser_id,
                    {name} if name else set(),
                    first,
                    last,
                )
                current = {
                    "mid": mid,
                    "candidates": candidates,
                    "preferred_count": preferred_count,
                    "next_batch": 0,
                    "search_stack": [],
                }
                progress["locate_post"] = current

            query_reader = QueryMembershipReader(
                read_ad_posts,
                progress.setdefault("locate_queries", {}),
                targets=stale,
                now=now,
            )

            def read_and_share(ads, first_day, last_day, page_number):
                result = query_reader(ads, first_day, last_day, page_number)
                if len(ads) == 1:
                    # 单广告响应已经证明整页帖子的归属，不再为每个帖子重复读取相同报表。
                    mids = {
                        str((row.get("dimensions") or {}).get("main_material_id") or "")
                        for row in result.rows
                    }
                    _upsert_links(
                        session,
                        [
                            _link(
                                tenant_id,
                                advertiser_id,
                                material_id=post,
                                ad_id=ads[0],
                                material_type=stale[post][1],
                                ad_material_id=None,
                                name=stale[post][0],
                                now=now,
                            )
                            for post in sorted(mids & set(stale))
                        ],
                    )
                return result

            complete, owner = _locate_post_step(
                current, read_ad_posts=read_and_share, first=first, last=last
            )
            if complete:
                if owner is not None:
                    _upsert_links(
                        session,
                        [
                            _link(
                                tenant_id,
                                advertiser_id,
                                material_id=mid,
                                ad_id=owner,
                                material_type=mtype,
                                ad_material_id=None,
                                name=name,
                                now=now,
                            )
                        ],
                    )
                checked.add(mid)
                progress["checked_ids"] = sorted(checked)
                progress.pop("locate_post", None)
            if not complete or len(remaining) > 1:
                _resolve_videos(session, tenant_id, advertiser_id, start=start, end=end)
                session.flush()
                return False
        if (
            progress.get("owner_lookup_first")
            and not progress.get("owner_lookup_finished")
            and not progress.get("overview_complete")
        ):
            # 最后一次归属读取后先让精确内容解析运行；此时不能签发概览缺失的负证据，
            # 也不能把新找到owner但尚待VID读取的帖子送入十分钟依赖退避。
            progress["owner_lookup_finished"] = True
            session.flush()
            return False
        unresolved = set(
            _unlinked(session, tenant_id, advertiser_id, start=start, end=end)
        )
        started_at = _scan_evidence_started_at(progress, now)
        if started_at is None:
            # 旧在途续点没有观察起点：保留正向映射与分页，不补造时间或覆盖
            # 其他轮次的负证据；调用方立即开新观察，既有等待进度重置会清除此标记。
            progress["scan_evidence_valid"] = False
        else:
            if scan is None:
                scan = MaterialLinkScan(
                    tenant_id=tenant_id,
                    advertiser_id=advertiser_id,
                    scanned_at=started_at,
                    query_scope=scope,
                )
            # 长排队不能把旧否定集合签为刚观察的新结果；有效期始终沿扫描起点。
            scan.unresolved_ids = sorted(unresolved)
            scan.scanned_at = started_at
            evidence_ids = progress.get("scan_evidence_ids", [])
            unchanged = (
                started_at > now - NEGATIVE_SCAN_TTL
                and unresolved <= set(evidence_ids)
                and progress.get("scan_evidence_signature")
                == _scan_signature(
                    session,
                    tenant_id,
                    advertiser_id,
                    evidence_ids,
                    start,
                    end,
                    overview_ad_ids,
                )
            )
            scan.evidence_signature = (
                _scan_signature(
                    session,
                    tenant_id,
                    advertiser_id,
                    unresolved,
                    start,
                    end,
                    overview_ad_ids,
                )
                if unchanged
                else None
            )
            session.add(scan)
        # 各查询范围独立缓存；仅回收已经失效的负证据，不触碰事实或缺失记录。
        session.connection().execute(
            text(
                "DELETE FROM material_link_scan WHERE tenant_id=:t AND advertiser_id=:a AND query_scope<>:s AND scanned_at<:expiry"
            ),
            {
                "t": tenant_id,
                "a": advertiser_id,
                "s": scope,
                "expiry": now - NEGATIVE_SCAN_TTL,
            },
        )
    _resolve_videos(session, tenant_id, advertiser_id, start=start, end=end)
    progress.pop("locate_queries", None)
    session.flush()
    return True
