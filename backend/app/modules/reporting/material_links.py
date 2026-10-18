"""素材日报主素材 → 广告 → 素材库视频的对照维护。

素材日报（拆分接口）有收入、能按天，但不带广告；Spark 广告的主素材 ID 是每条广告
各自发布的帖子 ID。创意概览接口能按“广告 × 主素材”返回同一 ID 及广告内素材 ID，
两者配合才能把每天的素材数据归到广告、再归到视频。

实测平台行为（2026-10-14，新骏伯生产账户）：
- 概览按帖子 ID 筛选不生效，无论日期都返回账户“当前”全部广告的素材，只能整账户拉。
- 广告内素材被重建时平台换发新帖子和新广告内素材 ID，概览不再列出旧帖子，但旧帖子
  已有的消耗仍在素材日报里；拆分接口按广告筛选时仍能查到旧帖子。
- 广告目录可能晚于平台更新，旧帖子与新帖子的广告内素材 ID 都可能对不上目录。

因此对照里直接记下视频 ID（建立对照时解析：先按广告内素材 ID，再按同一广告内的
文件名），查询时不再依赖目录当下的广告内素材 ID。概览查不到的旧帖子，只在同账户
用到同名文件的广告里逐条按广告查拆分报表定位，次数很少且一次定位后不再查。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import date, datetime
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

ReadAll = Callable[[Callable[[int], RawReportPage]], list[dict[str, Any]]]


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


def _unlinked(
    session: Session, tenant_id: UUID, advertiser_id: str
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
          AND NOT EXISTS (
            SELECT 1 FROM material_post_link l
            WHERE l.tenant_id = d.tenant_id AND l.advertiser_id = d.advertiser_id
              AND l.main_material_id = d.main_material_id)
        GROUP BY d.main_material_id
        """),
        {"t": tenant_id, "a": advertiser_id},
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
        WHERE tenant_id = :t AND advertiser_id = :a AND complete AND name = ANY(:names)
        ORDER BY ad_remote_id
        """),
        {"t": tenant_id, "a": advertiser_id, "names": sorted(names)},
    )
    return [r[0] for r in rows]


# 平台按广告筛选一次最多 200 个广告。
AD_FILTER_LIMIT = 200


def _locate_posts(
    ads: Sequence[str],
    targets: set[str],
    posts_of: Callable[[tuple[str, ...]], set[str]],
) -> dict[str, str]:
    """二分定位帖子所属广告：一个帖子只属于一条广告，查左半后，左半没有的目标必在右半，
    右半无需再查。请求次数约为“目标帖子数 × log2(广告数)”，不随无关广告线性增长。
    """
    owner: dict[str, str] = {}

    def search(batch: tuple[str, ...], present: set[str]) -> None:
        # present：已确认都在本批广告里的目标帖子。
        if not present:
            return
        if len(batch) == 1:
            for mid in present:
                owner[mid] = batch[0]
            return
        half = len(batch) // 2
        left = posts_of(batch[:half]) & present
        search(batch[:half], left)
        search(batch[half:], present - left)

    for start in range(0, len(ads), AD_FILTER_LIMIT):
        remaining = targets - set(owner)
        if not remaining:
            break
        batch = tuple(ads[start : start + AD_FILTER_LIMIT])
        search(batch, posts_of(batch) & remaining)
    return owner


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
    read_all: ReadAll,
    now: datetime,
) -> None:
    """在素材日报发布后调用；同一事务内补齐对照，读取失败随任务一起重试。"""
    unlinked = _unlinked(session, tenant_id, advertiser_id)
    scan = session.get(MaterialLinkScan, (tenant_id, advertiser_id))
    cached = scan is not None and set(unlinked) <= set(scan.unresolved_ids)
    if cached and unlinked:
        # 首次素材报表可能早于广告目录；新目录出现同名候选后，旧负缓存必须重试。
        # 没有新候选的已删除帖子仍保持缓存，避免每次热刷整账户重扫。
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
                      AND u.name=ANY(:names) AND a.observed_at>:scanned)
            """),
                {
                    "t": tenant_id,
                    "a": advertiser_id,
                    "names": names,
                    "scanned": scan.scanned_at,
                },
            )
            .scalar_one()
        )
        cached = not changed
    if unlinked and not cached:
        values = [
            _overview_link(row, tenant_id, advertiser_id, now)
            for row in read_all(read_overview)
        ]
        _upsert_links(session, values)
        found = {item["m"] for item in values}
        stale = {mid: meta for mid, meta in unlinked.items() if mid not in found}
        # 概览不再列出的旧帖子：只在同名文件所在广告里，按广告筛选拆分报表定位。
        if stale:
            first = min(meta[2] for meta in stale.values())
            last = max(meta[3] for meta in stale.values())
            names = {meta[0] for meta in stale.values() if meta[0]}
            owner = _locate_posts(
                _candidate_ads(session, tenant_id, advertiser_id, names),
                set(stale),
                lambda ads: {
                    str((row.get("dimensions") or {}).get("main_material_id") or "")
                    for row in read_all(
                        lambda page: read_ad_posts(ads, first, last, page)
                    )
                },
            )
            _upsert_links(
                session,
                [
                    _link(
                        tenant_id,
                        advertiser_id,
                        material_id=mid,
                        ad_id=ad_id,
                        material_type=stale[mid][1],
                        ad_material_id=None,
                        name=stale[mid][0],
                        now=now,
                    )
                    for mid, ad_id in sorted(owner.items())
                ],
            )
        unresolved = sorted(set(_unlinked(session, tenant_id, advertiser_id)))
        if scan is None:
            scan = MaterialLinkScan(
                tenant_id=tenant_id, advertiser_id=advertiser_id, scanned_at=now
            )
        scan.scanned_at = now
        scan.unresolved_ids = unresolved
        session.add(scan)
    # 目录同步可能晚于对照：每次都重试解析尚无视频的对照，只是数据库内更新。
    _resolve_videos(session, tenant_id, advertiser_id)
    session.flush()
