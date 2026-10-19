"""热槽只处理账户当前日；旧热窗口与历史修正共享独立补数容量。"""

from datetime import datetime

from sqlalchemy import text


def current_day_predicate(alias: str = "report_sync_job") -> str:
    # 别名仅由本模块的调用方提供，不接受任何请求输入；日期按各账户本地日判断。
    if alias not in {"j", "report_sync_job"}:
        raise ValueError("invalid report alias")
    return f"""({alias}.start_date={alias}.end_date AND NOT EXISTS (
          SELECT 1 FROM jsonb_array_elements_text({alias}.advertiser_ids) ids(advertiser_id)
          LEFT JOIN advertiser_account a ON a.tenant_id={alias}.tenant_id
            AND a.advertiser_id=ids.advertiser_id
          WHERE NULLIF(a.timezone, '') IS NULL OR {alias}.end_date <>
            (CAST(:lane_now AS timestamptz) AT TIME ZONE NULLIF(a.timezone, ''))::date))"""


def live_predicate(alias: str = "report_sync_job") -> str:
    day = current_day_predicate(alias)
    return f"({alias}.priority <= 2 AND {alias}.kind IN ('ad_day','material_day','campaign_hour') AND {day})"


def lane_predicate(*, lane: str, alias: str = "report_sync_job") -> str:
    predicate = live_predicate(alias)
    links = f"({alias}.kind = 'material_links' AND {current_day_predicate(alias)})"
    if lane == "live":
        return predicate
    if lane == "links":
        # 当前关联留在实时池，历史关联与历史事实共用独立补数池。
        return links
    if lane == "backfill":
        return f"NOT ({predicate} OR {links})"
    raise ValueError("invalid report lane")


def lane_condition(*, lane: str, now: datetime):
    condition = text(lane_predicate(lane=lane))
    return condition.bindparams(lane_now=now)


def report_workload(session, job, *, now):
    """实际执行时按账户当地日分类；午夜后旧热任务不会永久占用实时配额。"""
    if job.kind != "material_links" and job.priority > 2:
        return "background"
    current = (
        session.connection()
        .execute(
            text(
                f"SELECT {current_day_predicate()} FROM report_sync_job WHERE id=:job"
            ),
            {"job": job.id, "lane_now": now},
        )
        .scalar_one()
    )
    return "live" if current else "background"
