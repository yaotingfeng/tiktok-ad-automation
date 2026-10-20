"""今日、昨日收尾与历史补数分槽；旧窗口不能长期占用实时容量。"""

from datetime import datetime

from sqlalchemy import text

# 收尾独立实时档，不与今日热报争名额；数值只标识档位，派发先服务今日 lane。
CLOSEOUT_PRIORITY = 0


def _day_predicate(alias: str, *, days_ago: int) -> str:
    # 别名仅由本模块的调用方提供，不接受任何请求输入；日期按各账户本地日判断。
    if alias not in {"j", "report_sync_job"}:
        raise ValueError("invalid report alias")
    return f"""({alias}.start_date={alias}.end_date AND NOT EXISTS (
          SELECT 1 FROM jsonb_array_elements_text({alias}.advertiser_ids) ids(advertiser_id)
          LEFT JOIN advertiser_account a ON a.tenant_id={alias}.tenant_id
            AND a.advertiser_id=ids.advertiser_id
          WHERE NULLIF(a.timezone, '') IS NULL OR {alias}.end_date <>
            (CAST(:lane_now AS timestamptz) AT TIME ZONE NULLIF(a.timezone, ''))::date - {days_ago}))"""


def current_day_predicate(alias: str = "report_sync_job") -> str:
    return _day_predicate(alias, days_ago=0)


def closeout_predicate(alias: str = "report_sync_job") -> str:
    yesterday = _day_predicate(alias, days_ago=1)
    return f"({alias}.priority={CLOSEOUT_PRIORITY} AND {alias}.kind IN ('ad_day','material_day') AND {yesterday})"


def live_predicate(alias: str = "report_sync_job") -> str:
    day = current_day_predicate(alias)
    return f"({alias}.priority BETWEEN 1 AND 2 AND {alias}.kind IN ('ad_day','material_day','campaign_hour') AND {day})"


def lane_predicate(*, lane: str, alias: str = "report_sync_job") -> str:
    predicate = live_predicate(alias)
    links = f"({alias}.kind = 'material_links' AND {current_day_predicate(alias)})"
    if lane == "live":
        return predicate
    if lane == "closeout":
        return closeout_predicate(alias)
    if lane == "links":
        # 当前关联留在实时池，历史关联与历史事实共用独立补数池。
        return links
    if lane == "backfill":
        return f"NOT ({predicate} OR {links} OR {closeout_predicate(alias)})"
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
                f"SELECT ({current_day_predicate()} OR {closeout_predicate()}) FROM report_sync_job WHERE id=:job"
            ),
            {"job": job.id, "lane_now": now},
        )
        .scalar_one()
    )
    return "live" if current else "background"
