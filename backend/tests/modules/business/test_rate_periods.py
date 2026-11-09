from datetime import date
from decimal import Decimal as D

import pytest


def period(start, end, rate):
    from app.modules.business.rate_periods import RatePeriod

    return RatePeriod(start_date=start, end_date=end, rate=rate)


def test_earlier_unlimited_replaces_future_but_finite_preserves_it():
    from app.modules.business.rate_periods import RateOperation, transform_periods

    old = [period("2026-10-01", None, "0.04")]
    unlimited = transform_periods(
        old, RateOperation(start_date="2026-08-01", rate="0.06")
    )
    assert [(str(p.start_date), p.end_date, p.rate) for p in unlimited] == [
        ("2026-08-01", None, D("0.06"))
    ]
    finite = transform_periods(
        old, RateOperation(start_date="2026-08-01", end_date="2026-09-30", rate="0.06")
    )
    assert [p.rate for p in finite] == [D("0.06"), D("0.04")]


def test_overlay_splits_both_edges_and_explicit_zero():
    from app.modules.business.rate_periods import (
        RateOperation,
        period_rate,
        transform_periods,
    )

    old = [
        period("2026-08-01", "2026-09-30", "0.06"),
        period("2026-10-01", None, "0.04"),
    ]
    result = transform_periods(
        old, RateOperation(start_date="2026-09-01", end_date="2026-10-15", rate="0")
    )
    assert [p.rate for p in result] == [D("0.06"), D(0), D("0.04")]
    assert period_rate(result, date(2026, 10, 15)) == 0
    assert period_rate(result, date(2026, 10, 16)) == D("0.04")
    assert period_rate(result, date(2026, 7, 31)) == 0


def test_edit_move_and_shorten_retains_explicit_unknown_not_default():
    from app.modules.business.rate_periods import (
        RateOperation,
        period_rate,
        transform_periods,
    )

    old = [period("2026-08-01", None, "0.06")]
    result = transform_periods(
        old,
        RateOperation(
            action="edit",
            target_start_date="2026-08-01",
            start_date="2026-09-01",
            end_date="2026-09-30",
            rate="0.05",
        ),
    )
    assert [p.rate for p in result] == [None, D("0.05"), None]
    assert period_rate(result, date(2026, 8, 1)) is None
    assert period_rate(result, date(2026, 10, 1)) is None
    assert period_rate(result, date(2026, 7, 31)) == 0


def test_end_and_successor_have_explicit_boundaries():
    from app.modules.business.rate_periods import RateOperation, transform_periods

    old = [
        period("2026-08-01", "2026-09-30", "0.06"),
        period("2026-10-01", None, "0.04"),
    ]
    ended = transform_periods(
        old,
        RateOperation(
            action="end",
            target_start_date="2026-08-01",
            start_date="2026-08-01",
            end_date="2026-08-31",
        ),
    )
    assert [p.rate for p in ended] == [D("0.06"), None, D("0.04")]
    continued = transform_periods(
        old,
        RateOperation(
            action="end",
            target_start_date="2026-08-01",
            start_date="2026-08-01",
            end_date="2026-08-31",
            successor_rate="0.05",
            successor_end_date="2026-09-30",
        ),
    )
    assert [p.rate for p in continued] == [D("0.06"), D("0.05"), D("0.04")]
    forever = transform_periods(
        old,
        RateOperation(
            action="end",
            target_start_date="2026-08-01",
            start_date="2026-08-01",
            end_date="2026-08-31",
            successor_rate="0.05",
        ),
    )
    assert [p.rate for p in forever] == [D("0.06"), D("0.05")]


def test_first_finite_rate_ends_unknown_and_max_date_does_not_overflow():
    from app.modules.business.rate_periods import (
        RateOperation,
        period_rate,
        transform_periods,
    )

    result = transform_periods(
        [], RateOperation(start_date="2026-08-01", end_date="2026-08-31", rate="0.06")
    )
    assert period_rate(result, date(2026, 9, 1)) is None
    maximum = transform_periods(
        result, RateOperation(start_date="2026-09-01", end_date=date.max, rate="0.04")
    )
    assert period_rate(maximum, date.max) == D("0.04")


def test_invalid_ranges_and_stale_target_are_rejected():
    from app.modules.business.rate_periods import RateOperation, transform_periods

    with pytest.raises(ValueError):
        RateOperation(start_date="2026-09-01", end_date="2026-08-01", rate="0.06")
    with pytest.raises(ValueError):
        RateOperation(start_date="2026-09-01", rate="1.01")
    with pytest.raises(ValueError):
        transform_periods(
            [],
            RateOperation(
                action="edit",
                target_start_date="2026-08-01",
                start_date="2026-09-01",
                rate="0.06",
            ),
        )
