from pathlib import Path

from app.modules.reporting import metrics
from app.modules.reporting.metric_catalog import METRIC_CATALOG, metric_name_zh
from app.modules.reporting.schemas import CANONICAL_METRIC_KEYS

DOC = (
    Path(__file__).resolve().parents[4]
    / "docs/contracts/tiktok-basic-report-metrics/v1.3.zh.md"
).read_text()


def test_catalog_covers_every_canonical_metric():
    assert {item.key for item in METRIC_CATALOG} == set(CANONICAL_METRIC_KEYS)


def test_platform_fields_and_names_exist_in_official_doc():
    # 字典里的每个平台字段及其中文名都必须能在官方原文中找到，防止自创名称。
    for item in METRIC_CATALOG:
        for field, name in (
            (item.basic_field, item.basic_name_zh),
            (item.material_field, item.material_name_zh),
        ):
            if field is not None:
                assert field in DOC, field
                assert name in DOC, name


def test_metrics_module_reads_catalog_fields():
    assert metrics._BASIC["revenue_d3"] == "native_growth_ad_revenue_value_d2"
    assert metrics._MATERIAL["ad_impression_events"] == "onsite_total_ad_impression"
    assert "revenue_d3" not in metrics._MATERIAL


def test_names_follow_official_day_numbering():
    assert metric_name_zh("roas_d3", "campaign") == "Native Growth 第 2 天广告收益 ROAS"
    assert metric_name_zh("revenue_total", "material") == "广告曝光总价值"


def test_saved_view_accepts_catalog_keys():
    from app.modules.reporting.preferences import validate_columns

    assert validate_columns(("name", "roas_d0", "cpm")) == ("name", "roas_d0", "cpm")
