"""帖子在指定窗口内的业务价值：补链与规则共用，不改变页面事实或归并。"""

from collections.abc import Sequence
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlmodel import Session

VIDEO_REPORT_TYPES = ("VIDEO_SPARK_ADS", "VIDEO_NON_SPARK_ADS")

# 先按每个窗口、账户、帖子合计，再判正值；不能逐日筛正数后相加，也不能用
# 重叠窗口重复累计金额。规则包含多个窗口时，任一窗口有价值就必须保护身份缺口。
MATERIAL_WINDOW_VALUES_SQL = """
    SELECT advertiser_id, main_material_id, bool_or(has_value) AS has_value
    FROM (
      SELECT w.ordinality, f.advertiser_id, f.main_material_id,
        (COALESCE(sum(f.spend), 0)>0 OR COALESCE(sum(f.revenue_total), 0)>0) AS has_value
      FROM unnest(CAST(:value_advertisers AS text[]), CAST(:value_starts AS date[]),
                  CAST(:value_ends AS date[])) WITH ORDINALITY AS w(adv,s,e,ordinality)
      JOIN material_report_day f ON f.tenant_id=:value_tenant AND f.advertiser_id=w.adv
        AND (CAST(:value_material_types AS text[]) IS NULL OR f.main_material_type=ANY(CAST(:value_material_types AS text[])))
        AND (w.s IS NULL OR f.stat_date>=w.s) AND (w.e IS NULL OR f.stat_date<=w.e)
      GROUP BY w.ordinality,f.advertiser_id,f.main_material_id
    ) per_window GROUP BY advertiser_id,main_material_id
"""


def material_value_parameters(
    tenant_id: UUID,
    windows: Sequence[tuple[str, date | None, date | None]],
    *,
    material_types: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    return {
        "value_tenant": tenant_id,
        "value_material_types": list(material_types)
        if material_types is not None
        else None,
        "value_advertisers": [w[0] for w in windows],
        "value_starts": [w[1] for w in windows],
        "value_ends": [w[2] for w in windows],
    }


def material_window_values(
    session: Session,
    tenant_id: UUID,
    windows: Sequence[tuple[str, date | None, date | None]],
    *,
    material_types: tuple[str, ...] | None = None,
) -> dict[tuple[str, str], bool]:
    rows = session.connection().execute(
        text(MATERIAL_WINDOW_VALUES_SQL),
        material_value_parameters(tenant_id, windows, material_types=material_types),
    )
    return {(r.advertiser_id, r.main_material_id): r.has_value for r in rows}


def valuable_material_condition(alias: str) -> str:
    """内部 SQL 复用同一窗口聚合关系；别名不得来自外部请求。"""
    if alias not in {"d", "f", "l"}:
        raise ValueError("invalid material alias")
    return f"""EXISTS (SELECT 1 FROM material_values v
        WHERE v.advertiser_id={alias}.advertiser_id
          AND v.main_material_id={alias}.main_material_id AND v.has_value)"""
