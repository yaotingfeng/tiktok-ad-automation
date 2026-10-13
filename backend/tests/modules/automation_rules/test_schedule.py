from datetime import UTC, datetime

from app.modules.automation_rules.schedule import next_run_at
from app.modules.automation_rules.schemas import Schedule

SH = "Asia/Shanghai"


def at(*args):
    return datetime(*args, tzinfo=UTC)


def test_every_30_minutes_aligns_to_slot():
    s = Schedule(frequency="30M", window="CONTINUOUS")
    assert next_run_at(s, after=at(2026, 10, 14, 1, 7), timezone=SH) == at(
        2026, 10, 14, 1, 30
    )
    assert next_run_at(s, after=at(2026, 10, 14, 1, 30), timezone=SH) == at(
        2026, 10, 14, 2, 0
    )


def test_four_hours_aligns_in_tenant_timezone():
    s = Schedule(frequency="4H", window="CONTINUOUS")
    # 上海 09:10 → 下一个 4 小时整点为上海 12:00 = UTC 04:00
    assert next_run_at(s, after=at(2026, 10, 14, 1, 10), timezone=SH) == at(
        2026, 10, 14, 4, 0
    )


def test_daily_at():
    s = Schedule(frequency="DAILY", daily_at="10:00", window="CONTINUOUS")
    assert next_run_at(s, after=at(2026, 10, 14, 3, 0), timezone=SH) == at(
        2026, 10, 15, 2, 0
    )


def test_custom_week_periods():
    s = Schedule.model_validate(
        {
            "frequency": "1H",
            "window": "CUSTOM",
            "periods": [
                {"date_type": "WEEK", "days": [1], "start": "09:00", "end": "10:00"}
            ],
        }
    )
    # 2026-10-14 是周三;下一次在周一上海 09:00 = UTC 01:00
    assert next_run_at(s, after=at(2026, 10, 14, 1, 0), timezone=SH) == at(
        2026, 10, 19, 1, 0
    )
    assert next_run_at(s, after=at(2026, 10, 19, 1, 0), timezone=SH) == at(
        2026, 10, 19, 2, 0
    )
    assert next_run_at(s, after=at(2026, 10, 19, 2, 0), timezone=SH) == at(
        2026, 10, 26, 1, 0
    )


def test_once():
    s = Schedule(frequency="30M", window="ONCE", once_at=at(2026, 10, 20, 0, 0))
    assert next_run_at(s, after=at(2026, 10, 14, 0, 0), timezone=SH) == at(
        2026, 10, 20, 0, 0
    )
    assert next_run_at(s, after=at(2026, 10, 20, 0, 0), timezone=SH) is None
