"""Capacity evidence must come from real modules and bounded page consumption."""

import importlib.util
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.jobs.celery_app import celery_app


def test_capacity_harness_exists():
    assert importlib.util.find_spec("scripts.benchmark_batches"), (
        "Isolated capacity harness is missing"
    )


def test_default_bc_age_covers_100k_serial_read_and_publication_pages():
    cadence = celery_app.conf.beat_schedule["flush-dispatch"]["schedule"]
    minimum_seconds = ((100_000 + 49) // 50 + (100_000 + 99) // 100) * cadence
    default = Settings.model_fields["BC_CAPABILITY_MAX_AGE_SECONDS"].default
    assert default >= minimum_seconds * 2, (
        "Default BC evidence expires before a 100k directory completes with reasonable scheduling margin"
    )


def test_page_measurement_consumes_without_collecting_rows_and_rejects_duplicates():
    from scripts.benchmark_batches import measure_pages

    seen = []

    def fetch(cursor):
        number = int(cursor or 0)
        seen.append(number)
        return SimpleNamespace(
            items=[{"id": f"{number:04}-{i}"} for i in range(3)],
            next_cursor=str(number + 1) if number < 4 else None,
        )

    result = measure_pages(fetch, identity=lambda row: row["id"])
    assert result["rows"] == 15 and result["page_max"] == 3
    assert result["pages"] == 5 and result["max_response_bytes"] > 0
    assert "items" not in result and seen == list(range(5))
    with pytest.raises(ValueError, match="ordered"):
        measure_pages(
            lambda cursor: SimpleNamespace(
                items=[{"id": "same"}, {"id": "same"}], next_cursor=None
            ),
            identity=lambda row: row["id"],
        )


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://localhost/business",
        "postgresql://localhost/my_test_backup",
        "sqlite:///p07_test",
    ],
)
def test_benchmark_rejects_nonexplicit_test_database_before_connecting(url):
    from scripts.benchmark_batches import validate_database_url

    with pytest.raises(ValueError):
        validate_database_url(url)


def test_small_benchmark_generates_real_frozen_plan_and_submission(tmp_path):
    from scripts.benchmark_batches import Parameters, run_benchmark

    result = run_benchmark(
        Parameters(accounts=12, dramas=2, target_accounts=2),
        output=tmp_path / "capacity.json",
    )
    assert result["complete"]
    assert result["plan_counts"] == {"campaign": 4, "group": 12, "ad": 24}
    preview = result["phases"]["preview"]["summary"]
    assert preview["status"] == "FROZEN" and preview["blocked_count"] == 0
    assert preview["daily_budget_sum"] == "400.000000000000"
    assert result["phases"]["submission"]["steps"]["CAMPAIGN"] == 4
    assert result["phases"]["submission"]["steps"]["MATERIAL"] == 120
    assert result["phases"]["directory"]["rows"] == 12
    assert result["phases"]["preview_pages"]["rows"] == 4
    calls = result["transport_call_counts"]
    assert calls["/open_api/v1.3/bc/asset/get/"] == 1
    assert calls["/open_api/v1.3/identity/get/"] == 2
    assert not any("/create/" in name for name in calls)
