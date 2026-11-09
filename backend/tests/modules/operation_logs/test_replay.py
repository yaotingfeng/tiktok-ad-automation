from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.modules.operation_logs.replay import evaluate_day, restore_bid

T = datetime(2026, 11, 1, tzinfo=UTC)


def change(before, after, minute=1):
    return {
        "at": T + timedelta(minutes=minute),
        "before": Decimal(before),
        "after": Decimal(after),
    }


def test_backward_minimum_and_same_minute_chain():
    assert restore_bid(
        Decimal("1.1"),
        T + timedelta(days=1),
        T,
        T + timedelta(days=1),
        [change("1.3", "1.2"), change("1.2", "1.1")],
    ) == Decimal("1.1")
    assert restore_bid(
        Decimal("1.3"),
        T + timedelta(days=1),
        T,
        T + timedelta(days=1),
        [change("1.1", "1.3")],
    ) == Decimal("1.1")


def test_missing_or_ambiguous_chain_is_unknown():
    assert (
        restore_bid(
            Decimal("1.5"),
            T + timedelta(days=1),
            T,
            T + timedelta(days=1),
            [change("1.1", "1.3")],
        )
        is None
    )
    assert (
        restore_bid(
            Decimal("1.1"),
            T + timedelta(days=1),
            T,
            T + timedelta(days=1),
            [change("1.1", "1.1"), change("1.1", "1.1")],
        )
        is None
    )


def test_deterministic_zero_and_pending_qualification():
    assert (
        evaluate_day(spend=Decimal(0), events=None, revenue=None, bid=None, reasons=[])[
            "amount"
        ]
        == "0"
    )
    assert (
        evaluate_day(
            spend=Decimal(100), events=Decimal(299), revenue=None, bid=None, reasons=[]
        )["amount"]
        == "0"
    )
    result = evaluate_day(
        spend=Decimal(100),
        events=Decimal(300),
        revenue=Decimal(60),
        bid=Decimal("1.2"),
        reasons=[],
    )
    assert result["amount"] == "50"
    assert result["status"] == "ESTIMATED_PENDING"
    assert (
        evaluate_day(
            spend=Decimal(100),
            events=Decimal(300),
            revenue=Decimal(100),
            bid=Decimal("1.2"),
            reasons=[],
        )["amount"]
        == "0"
    )
    assert (
        evaluate_day(
            spend=Decimal(100),
            events=None,
            revenue=None,
            bid=None,
            reasons=["CAMPAIGN_PAUSED"],
        )["amount"]
        == "0"
    )


def test_forward_anchor_before_day_does_not_include_old_lower_bid():
    changes = [
        {
            "at": T - timedelta(hours=1),
            "before": Decimal("1.1"),
            "after": Decimal("1.3"),
        }
    ]
    assert restore_bid(
        Decimal("1.1"), T - timedelta(days=1), T, T + timedelta(days=1), changes
    ) == Decimal("1.3")


def test_other_structure_cannot_use_iaa_event_threshold():
    assert (
        evaluate_day(
            spend=Decimal(100),
            events=Decimal(1),
            revenue=Decimal(0),
            bid=Decimal("1.2"),
            reasons=["STRUCTURE_UNSUPPORTED"],
        )["amount"]
        is None
    )
