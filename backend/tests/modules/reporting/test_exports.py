from uuid import uuid4

import pytest

from app.modules.reporting.exports import _csv_rows, csv_safe_text
from app.modules.reporting.query_models import ReportExport


@pytest.mark.parametrize("value", ["=1+1", "+SUM(A1)", "-cmd", "@x", "\t=1"])
def test_csv_safe_text_protects_formula_prefix(value: str):
    assert csv_safe_text(value).startswith("'")


def test_csv_keeps_decimal_amount_numeric():
    export = ReportExport(
        id=uuid4(),
        tenant_id=uuid4(),
        bc_id="bc",
        actor_id=uuid4(),
        advertiser_ids=["adv"],
        filters={},
        filter_digest="a" * 64,
        publication_versions={},
        naming_versions={},
        snapshot_id=uuid4(),
        idempotency_key="k",
        frozen_rows=[
            {
                "row_key": "campaign-1",
                "display": {"name": "=formula", "advertiser_id": "adv"},
                "metric_buckets": [
                    {
                        "currency": "USD",
                        "timezone": "UTC",
                        "values": {"spend": "-2", "d0_roas": "1.2"},
                    }
                ],
            }
        ],
        coverage={"status": "COMPLETE"},
    )
    text = _csv_rows(export).decode()
    assert "'=formula" in text
    assert ",-2," in text
    assert "USD" in text and "UTC" in text and "COMPLETE" in text
