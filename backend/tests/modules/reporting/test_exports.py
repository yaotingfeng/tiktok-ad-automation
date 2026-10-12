from uuid import uuid4

import pytest

from app.modules.reporting.exports import _csv_rows, csv_safe_text
from app.modules.reporting.query_models import ReportExport


def _export_like_existing_test(frozen_rows, dimension="campaign"):
    """构造测试用导出对象，维度可配置。"""
    return ReportExport(
        id=uuid4(),
        tenant_id=uuid4(),
        bc_id="bc",
        actor_id=uuid4(),
        advertiser_ids=["adv"],
        filters={"dimension": dimension},
        filter_digest="a" * 64,
        publication_versions={},
        naming_versions={},
        snapshot_id=uuid4(),
        idempotency_key="k",
        frozen_rows=frozen_rows,
        coverage={"status": "COMPLETE"},
    )


@pytest.mark.parametrize("value", ["=1+1", "+SUM(A1)", "-cmd", "@x", "\t=1"])
def test_csv_safe_text_protects_formula_prefix(value: str):
    assert csv_safe_text(value).startswith("'")


def test_csv_keeps_decimal_amount_numeric():
    export = _export_like_existing_test(
        frozen_rows=[
            {
                "row_key": "campaign-1",
                "display": {"name": "=formula", "advertiser_id": "adv"},
                "metric_buckets": [
                    {
                        "currency": "USD",
                        "timezone": "UTC",
                        "values": {"spend": "-2", "roas_d0": "1.2"},
                    }
                ],
            }
        ]
    )
    text = _csv_rows(export).decode()
    assert "'=formula" in text
    assert ",-2," in text
    assert "USD" in text and "UTC" in text and "COMPLETE" in text


def test_balance_export_keeps_observation_currency_out_of_period_metrics():
    import csv
    import io

    export = ReportExport(
        tenant_id=uuid4(),
        bc_id="bc",
        actor_id=uuid4(),
        advertiser_ids=["a"],
        filters={},
        filter_digest="a" * 64,
        publication_versions={},
        naming_versions={},
        snapshot_id=uuid4(),
        idempotency_key="balance",
        frozen_rows=[
            {
                "row_key": "a",
                "display": {
                    "balance": "12.50",
                    "balance_currency": "CNY",
                    "balance_scope": "PORTFOLIO",
                    "balance_updated_at": "2026-10-12T00:00:00Z",
                },
                "metric_buckets": [
                    {"currency": "USD", "timezone": "UTC", "values": {"spend": "10"}}
                ],
            }
        ],
        coverage={},
    )
    row = next(csv.DictReader(io.StringIO(_csv_rows(export).decode())))
    assert row["balance"] == "12.50" and row["balance_currency"] == "CNY"
    assert (
        row["balance_scope"] == "PORTFOLIO"
        and row["消耗"] == "10"
        and row["currency"] == "USD"
    )


def test_csv_header_uses_official_names():
    export = _export_like_existing_test(frozen_rows=[], dimension="campaign")
    header = _csv_rows(export).decode().splitlines()[0].split(",")
    assert "消耗" in header
    assert "Native Growth 第 2 天广告收益 ROAS" in header
    assert "roas_d3" not in header
