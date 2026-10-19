"""素材日报主素材 → 广告 → 素材库视频的对照维护。

素材日报（拆分接口）有收入、能按天，但不带广告；Spark 广告的主素材 ID 是每条广告
各自发布的帖子 ID。创意概览接口能按“广告 × 主素材”返回同一 ID 及广告内素材 ID，
两者配合才能把每天的素材数据归到广告、再归到视频。

实测平台行为（2026-10-14，新骏伯生产账户）：
- 概览按帖子 ID 筛选不生效；按已确认广告 ID 可筛选当前素材，日期参数不能限定帖子。
- 广告内素材被重建时平台换发新帖子和新广告内素材 ID，概览不再列出旧帖子，但旧帖子
  已有的消耗仍在素材日报里；拆分接口按广告筛选时仍能查到旧帖子。
- 广告目录可能晚于平台更新，旧帖子与新帖子的广告内素材 ID 都可能对不上目录。

因此对照里直接记下视频 ID（建立对照时解析：先按广告内素材 ID，再按同一广告内的
文件名），查询时不再依赖目录当下的广告内素材 ID。概览查不到的旧帖子，只在同账户
用到同名文件的广告里逐条按广告查拆分报表定位，次数很少且一次定位后不再查。
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
from app.modules.reporting.store_models import MaterialLinkScan

# 主素材（帖子）→ 视频；同一主素材对应多条广告（非 Spark 共用）时取最小视频 ID，
# 保证每笔金额只归入一个视频。依赖参数 :tenant、:advs。
POST_VIDEO_SQL = """
    SELECT DISTINCT ON (l.advertiser_id, l.main_material_id)
        l.advertiser_id AS adv, l.main_material_id AS mid, l.platform_material_id AS vid
    FROM material_post_link l
    WHERE l.tenant_id = :tenant AND l.advertiser_id = ANY(:advs)
      AND l.platform_material_id IS NOT NULL
    ORDER BY l.advertiser_id, l.main_material_id, l.platform_material_id COLLATE "C"
"""


def material_row_key(adv: str, mid: str, vid: str | None) -> str:
    """素材维度行键：对上视频的按视频跨账户一行；对不上的帖子单独一行，金额不丢。"""
    return json.dumps(["material", vid] if vid else ["post", adv, mid])


def post_video_map(
    session: Session, *, tenant_id: UUID, advertisers: Sequence[str]
) -> dict[tuple[str, str], str]:
    rows = session.connection().execute(
        text(POST_VIDEO_SQL), {"tenant": tenant_id, "advs": sorted(advertisers)}
    )
    return {(r.adv, r.mid): r.vid for r in rows}


def needs_material_links(
    session: Session,
    *,
    tenant_id: UUID,
    advertiser_id: str,
    start: date,
    end: date,
) -> bool:
    """仅真实事实缺有效视频映射时排关联；零消费也需要对照，空窗口不制造空跑。"""
    return bool(
        session.connection()
        .execute(
            text("""
            SELECT EXISTS (
                SELECT 1 FROM material_report_day d
                WHERE d.tenant_id=:tenant AND d.advertiser_id=:advertiser
                  AND d.stat_date BETWEEN :start AND :end
                  AND NOT EXISTS (
                      SELECT 1 FROM material_post_link l
                      WHERE l.tenant_id=d.tenant_id AND l.advertiser_id=d.advertiser_id
                        AND l.main_material_id=d.main_material_id
                        AND NULLIF(l.platform_material_id, '') IS NOT NULL))
            """),
            {
                "tenant": tenant_id,
                "advertiser": advertiser_id,
                "start": start,
                "end": end,
            },
        )
        .scalar_one()
    )


# 扫描结束与映射完整是两件事；依赖不齐保留一条等待工作，不能完成后每轮重造。
DEPENDENCY_ERRORS = {"material_directory_pending", "material_identity_unavailable"}
DEPENDENCY_RECHECK = timedelta(minutes=10)


def missing_video_ids(session, *, tenant_id, advertiser_id, start, end):
    return set(
        session.connection()
        .execute(
            text("""
        SELECT DISTINCT d.main_material_id FROM material_report_day d
        WHERE d.tenant_id=:t AND d.advertiser_id=:a AND d.stat_date BETWEEN :start AND :end
          AND NOT EXISTS (SELECT 1 FROM material_post_link l
            WHERE l.tenant_id=d.tenant_id AND l.advertiser_id=d.advertiser_id
              AND l.main_material_id=d.main_material_id
              AND NULLIF(l.platform_material_id, '') IS NOT NULL)
    """),
            {"t": tenant_id, "a": advertiser_id, "start": start, "end": end},
        )
        .scalars()
    )


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
            text("""
        WITH missing AS MATERIALIZED (
          SELECT DISTINCT d.main_material_id, d.main_material_name
          FROM material_report_day d
          WHERE d.tenant_id=:t AND d.advertiser_id=:a AND d.stat_date BETWEEN :start AND :end
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
          FROM (SELECT DISTINCT m.main_material_name FROM missing m
                WHERE m.main_material_name IS NOT NULL AND NOT EXISTS (
                  SELECT 1 FROM material_post_link l WHERE l.tenant_id=:t
                    AND l.advertiser_id=:a AND l.main_material_id=m.main_material_id)) names
          JOIN ad_material_reference u ON u.tenant_id=:t AND u.advertiser_id=:a
            AND u.complete AND md5(u.name)=md5(names.main_material_name)
            AND u.name=names.main_material_name),
        evidence AS (
          SELECT jsonb_build_array(ad_remote_id, ad_material_id, platform_material_id, name)::text AS value
          FROM candidates)
        SELECT md5(COALESCE(string_agg(value, ',' ORDER BY value), '')) FROM evidence
    """),
            {
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
    job.error_code = (
        "material_identity_unavailable" if unlinked else "material_directory_pending"
    )
    from app.modules.reporting.directory_refresh import request_link_directory

    request_link_directory(session, job, now=now)
    job.due_at = now + DEPENDENCY_RECHECK
    # 到期重读时重新确定查询范围；此前验证的映射已经持久化，不回滚。
    job.progress = {
        key: value
        for key, value in job.progress.items()
        if key in {"coalesced_v1", "requested_at", "initial_window"}
    } | {
        "waiting_ids": sorted(missing),
        "dependency_checked_at": now.isoformat(),
        "dependency_signature": directory_signature(session, job),
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
        text("""
        SELECT d.main_material_id AS mid,
            (array_agg(d.main_material_name ORDER BY d.stat_date DESC)
                FILTER (WHERE d.main_material_name IS NOT NULL))[1] AS name,
            min(d.main_material_type) AS mtype, min(d.stat_date) AS first_day,
            max(d.stat_date) AS last_day
        FROM material_report_day d
        WHERE d.tenant_id = :t AND d.advertiser_id = :a
          AND (CAST(:start AS date) IS NULL OR d.stat_date >= :start)
          AND (CAST(:end AS date) IS NULL OR d.stat_date <= :end)
          AND NOT EXISTS (
            SELECT 1 FROM material_post_link l
            WHERE l.tenant_id = d.tenant_id AND l.advertiser_id = d.advertiser_id
              AND l.main_material_id = d.main_material_id)
        GROUP BY d.main_material_id
        """),
        {"t": tenant_id, "a": advertiser_id, "start": start, "end": end},
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
    session: Session, tenant_id: UUID, advertiser_id: str, names: set[str]
) -> list[str]:
    """同账户里用到这些文件名的广告：旧帖子只可能属于这些广告。"""
    if not names:
        return []
    rows = session.connection().execute(
        text("""
        SELECT DISTINCT ad_remote_id FROM ad_material_reference
        WHERE tenant_id = :t AND advertiser_id = :a AND complete
          AND md5(name) = ANY(:name_hashes) AND name = ANY(:names)
        ORDER BY ad_remote_id
        """),
        {
            "t": tenant_id,
            "a": advertiser_id,
            "names": sorted(names),
            "name_hashes": [
                hashlib.md5(name.encode()).hexdigest() for name in sorted(names)
            ],
        },
    )
    return [r[0] for r in rows]


# 平台按广告筛选一次最多 200 个广告。
AD_FILTER_LIMIT = 200


def _locate_post_step(state, *, read_ad_posts, first, last):
    """单帖二分路径与当前查询页持久化；每步最多一次平台读取。

    已确认包含目标的右半无需读取。栈仅保留尚未定位的路径，避免把每次完整
    二分查找塞进一个期限；跨 200 广告批次、分页也分别提交后再继续。
    """
    stack = state["search_stack"]
    if not stack:
        offset = state["next_batch"]
        batch = state["candidates"][offset : offset + AD_FILTER_LIMIT]
        if not batch:
            return True, None
        stack.append({"ads": batch, "confirmed": False})
        state["next_batch"] += len(batch)
    node = stack[-1]
    ads = node["ads"]
    query_ads = ads[: len(ads) // 2] if node["confirmed"] else ads
    query = state.get("query", {"page": 1, "found": False})
    result = read_ad_posts(tuple(query_ads), first, last, query["page"])
    if result.total_number >= 20_000:
        raise DomainError("report_truncation_risk", "报表行数达到平台上限，可能被截断")
    signature = [result.total_page, result.total_number]
    if query.get("signature", signature) != signature:
        # 翻页期间集合改变时只重读当前子集；不能把不完整的缺失推导为右半命中。
        state.pop("query", None)
        return False, None
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
    if node["confirmed"]:
        node["ads"] = query_ads if query["found"] else ads[len(ads) // 2 :]
    elif query["found"]:
        node["confirmed"] = True
    else:
        stack.pop()
        return state["next_batch"] >= len(state["candidates"]), None
    if len(node["ads"]) == 1:
        return True, node["ads"][0]
    return False, None


def _resolve_videos(session: Session, tenant_id: UUID, advertiser_id: str) -> None:
    """为尚无视频的对照解析视频：先按广告内素材 ID，再按同一广告内唯一的同名文件。"""
    session.connection().execute(
        text("""
        UPDATE material_post_link l SET platform_material_id = COALESCE(
            (SELECT min(u.platform_material_id) FROM ad_material_reference u
              WHERE u.tenant_id = l.tenant_id AND u.advertiser_id = l.advertiser_id
                AND u.ad_remote_id = l.ad_remote_id AND u.ad_material_id = l.ad_material_id
                AND u.complete),
            (SELECT CASE WHEN count(DISTINCT u.platform_material_id) = 1
                    THEN min(u.platform_material_id) END
               FROM ad_material_reference u
              WHERE u.tenant_id = l.tenant_id AND u.advertiser_id = l.advertiser_id
                AND u.ad_remote_id = l.ad_remote_id AND u.name = l.material_name
                AND u.complete))
        WHERE l.tenant_id = :t AND l.advertiser_id = :a AND l.platform_material_id IS NULL
        """),
        {"t": tenant_id, "a": advertiser_id},
    )


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
) -> bool:
    """推进有界关联扫描；返回是否完成，调用方把游标、已解析关联一起提交。

    当前日报只修其日期内的帖子，旧帖子由补数扫描处理；概览逐页保存，不能因一个
    历史帖子的定位超时反复重读整账户。缺少目录的帖子保留真实缺口，不能填零。
    """
    progress = progress if progress is not None else {}
    unlinked = _unlinked(session, tenant_id, advertiser_id, start=start, end=end)
    scan = session.get(MaterialLinkScan, (tenant_id, advertiser_id))
    cached = scan is not None and set(unlinked) <= set(scan.unresolved_ids)
    if cached and unlinked:
        names = sorted({meta[0] for meta in unlinked.values() if meta[0]})
        changed = (
            session.connection()
            .execute(
                text("""
            SELECT EXISTS (
                SELECT 1 FROM ad_material_reference u JOIN ad_object a
                ON a.tenant_id=u.tenant_id AND a.advertiser_id=u.advertiser_id
                  AND a.kind='ad' AND a.remote_id=u.ad_remote_id
                WHERE u.tenant_id=:t AND u.advertiser_id=:a AND u.complete
                  AND md5(u.name)=ANY(:name_hashes) AND u.name=ANY(:names) AND a.observed_at>:scanned)
        """),
                {
                    "t": tenant_id,
                    "a": advertiser_id,
                    "names": names,
                    "name_hashes": [
                        hashlib.md5(name.encode()).hexdigest() for name in names
                    ],
                    "scanned": scan.scanned_at,
                },
            )
            .scalar_one()
        )
        # 无新候选仍定期复核：目录或平台概览可能在最初读取后才变得完整。
        cached = not changed and scan.scanned_at >= now - timedelta(minutes=10)
    if unlinked and not cached:
        if not progress.get("overview_complete"):
            page = read_overview(int(progress.get("next_page", 1)))
            _upsert_links(
                session,
                [
                    _overview_link(row, tenant_id, advertiser_id, now)
                    for row in page.rows
                ],
            )
            if page.next_page is None:
                progress["overview_complete"] = True
            else:
                progress["next_page"] = page.next_page
            # 包括概览终页都先独立落库，后面的旧帖定位异常不能撤回这些页。
            _resolve_videos(session, tenant_id, advertiser_id)
            session.flush()
            return bool(progress.get("overview_complete")) and not _unlinked(
                session, tenant_id, advertiser_id, start=start, end=end
            )
        stale = _unlinked(session, tenant_id, advertiser_id, start=start, end=end)
        checked = set(progress.get("checked_ids", []))
        remaining = sorted(set(stale) - checked)
        if remaining:
            current = progress.get("locate_post")
            mid = (
                current["mid"]
                if current and current["mid"] in remaining
                else remaining[0]
            )
            name, mtype, first, last = stale[mid]
            if not current or current["mid"] != mid:
                current = {
                    "mid": mid,
                    "candidates": _candidate_ads(
                        session, tenant_id, advertiser_id, {name} if name else set()
                    ),
                    "next_batch": 0,
                    "search_stack": [],
                }
                progress["locate_post"] = current

            def read_and_share(ads, first_day, last_day, page_number):
                result = read_ad_posts(ads, first_day, last_day, page_number)
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
                _resolve_videos(session, tenant_id, advertiser_id)
                session.flush()
                return False
        unresolved = set(
            _unlinked(session, tenant_id, advertiser_id, start=start, end=end)
        )
        if scan is None:
            scan = MaterialLinkScan(
                tenant_id=tenant_id, advertiser_id=advertiser_id, scanned_at=now
            )
        # 时间戳只属于本次已完整扫描的集合；不可用新窗口给旧窗口负证据无限续期。
        scan.unresolved_ids = sorted(unresolved)
        scan.scanned_at = now
        session.add(scan)
    _resolve_videos(session, tenant_id, advertiser_id)
    session.flush()
    return True
