from datetime import UTC, datetime, timedelta

import pytest

from app.modules.ai_workbench.review_authorization import claim_expiry


@pytest.mark.parametrize("seconds", [30, 300, 900])
def test_queue_delay_does_not_consume_original_execution_window(seconds):
    created = datetime(2026, 11, 13, tzinfo=UTC)
    now = created + timedelta(seconds=seconds - 10)
    assert claim_expiry(
        created_at=created,
        expires_at=created + timedelta(seconds=seconds),
        restart_generation=0,
        now=now,
    ) == now + timedelta(seconds=seconds)


def test_recovered_review_gets_existing_five_minute_grant_from_claim():
    created = datetime(2026, 11, 13, tzinfo=UTC)
    now = created + timedelta(days=1)
    assert claim_expiry(
        created_at=created,
        expires_at=now - timedelta(seconds=10),
        restart_generation=2,
        now=now,
    ) == now + timedelta(minutes=5)
