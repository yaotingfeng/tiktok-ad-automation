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
    if lane == "live":
        return predicate
    if lane == "links":
        # 关联独立于事实窗口；派发器在此窗口内为当前/历史分别保留服务份额。
        return f"{alias}.kind = 'material_links'"
    if lane == "backfill":
        return f"(NOT {predicate} AND {alias}.kind <> 'material_links')"
    raise ValueError("invalid report lane")


def lane_condition(*, lane: str, now: datetime):
    condition = text(lane_predicate(lane=lane))
    return condition if lane == "links" else condition.bindparams(lane_now=now)
