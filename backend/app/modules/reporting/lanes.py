"""热槽只处理账户当前日；旧热窗口与历史修正共享独立补数容量。"""

from datetime import datetime

from sqlalchemy import text


def live_predicate(alias: str = "report_sync_job") -> str:
    # 别名仅由本模块的调用方提供，不接受任何请求输入；日期按各账户本地日判断。
    if alias not in {"j", "report_sync_job"}:
        raise ValueError("invalid report alias")
    return f"""({alias}.priority <= 2 AND {alias}.start_date={alias}.end_date AND {alias}.kind IN
        ('ad_day','material_day','material_links','campaign_hour') AND NOT EXISTS (
          SELECT 1 FROM jsonb_array_elements_text({alias}.advertiser_ids) ids(advertiser_id)
          LEFT JOIN advertiser_account a ON a.tenant_id={alias}.tenant_id
            AND a.advertiser_id=ids.advertiser_id
          WHERE a.timezone IS NULL OR {alias}.end_date <>
            (CAST(:lane_now AS timestamptz) AT TIME ZONE a.timezone)::date))"""


def lane_condition(*, lane: str, now: datetime):
    if lane not in {"live", "backfill"}:
        raise ValueError("invalid report lane")
    predicate = live_predicate()
    return text(predicate if lane == "live" else f"NOT {predicate}").bindparams(
        lane_now=now
    )
