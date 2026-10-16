from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.modules.automation_rules.schedule import next_run_at
from app.modules.automation_rules.schemas import Schedule

SH = "Asia/Shanghai"


def at(*args):
    return datetime(*args, tzinfo=UTC)


def test_every_30_minutes_aligns_to_slot():
    s = Schedule(frequency="30M", window="CONTINUOUS")
    assert next_run_at(s, after=at(2026, 10, 14, 1, 7), timezones=[SH]) == at(
        2026, 10, 14, 1, 30
    )
    assert next_run_at(s, after=at(2026, 10, 14, 1, 30), timezones=[SH]) == at(
        2026, 10, 14, 2, 0
    )


def test_four_hours_aligns_in_tenant_timezone():
    s = Schedule(frequency="4H", window="CONTINUOUS")
    # 上海 09:10 → 下一个 4 小时整点为上海 12:00 = UTC 04:00
    assert next_run_at(s, after=at(2026, 10, 14, 1, 10), timezones=[SH]) == at(
        2026, 10, 14, 4, 0
    )


def test_daily_at():
    s = Schedule(frequency="DAILY", daily_at="10:00", window="CONTINUOUS")
    assert next_run_at(s, after=at(2026, 10, 14, 3, 0), timezones=[SH]) == at(
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
    assert next_run_at(s, after=at(2026, 10, 14, 1, 0), timezones=[SH]) == at(
        2026, 10, 19, 1, 0
    )
    assert next_run_at(s, after=at(2026, 10, 19, 1, 0), timezones=[SH]) == at(
        2026, 10, 19, 2, 0
    )
    assert next_run_at(s, after=at(2026, 10, 19, 2, 0), timezones=[SH]) == at(
        2026, 10, 26, 1, 0
    )


def test_once():
    s = Schedule(frequency="30M", window="ONCE", once_at=at(2026, 10, 20, 0, 0))
    assert next_run_at(s, after=at(2026, 10, 14, 0, 0), timezones=[SH]) == at(
        2026, 10, 20, 0, 0
    )
    assert next_run_at(s, after=at(2026, 10, 20, 0, 0), timezones=[SH]) is None


def _walk(schedule, start, end, timezone):
    runs, cursor = [], start
    while True:
        cursor = next_run_at(schedule, after=cursor, timezones=[timezone])
        if cursor is None or cursor >= end:
            return runs
        runs.append(cursor)


def test_dst_spring_forward_no_duplicate_or_skip():
    from app.modules.automation_rules.schemas import Schedule

    schedule = Schedule.model_validate({"frequency": "30M", "window": "CONTINUOUS"})
    # 纽约 2026-03-08 02:00 跳到 03:00：当天只有 23 小时，即 46 个槽位且无重复。
    start = datetime(2026, 3, 8, 5, 0, tzinfo=UTC) - timedelta(seconds=1)
    end = datetime(2026, 3, 9, 4, 0, tzinfo=UTC)
    runs = _walk(schedule, start, end, "America/New_York")
    assert len(runs) == len(set(runs)) == 46


def test_dst_fall_back_runs_each_wall_slot_once():
    from app.modules.automation_rules.schemas import Schedule

    schedule = Schedule.model_validate(
        {"frequency": "DAILY", "daily_at": "01:30", "window": "CONTINUOUS"}
    )
    # 纽约 2026-11-01 01:30 出现两次，每天固定时刻只运行一次。
    start = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    end = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
    runs = _walk(schedule, start, end, "America/New_York")
    assert [run.astimezone(ZoneInfo("America/New_York")).day for run in runs] == [
        1,
        2,
        3,
    ]


def test_account_timezones_take_the_earliest_local_slot():
    from app.modules.automation_rules.schedule import due_at

    s = Schedule(
        frequency="DAILY",
        daily_at="10:00",
        window="CONTINUOUS",
        timezone_basis="ACCOUNT",
    )
    zones = ["Etc/GMT", "Etc/GMT+8", "Africa/Accra"]
    # UTC 账户当地 10:00 = UTC 10:00；UTC-8 账户当地 10:00 = UTC 18:00。
    first = next_run_at(s, after=at(2026, 10, 14, 3, 0), timezones=zones)
    assert first == at(2026, 10, 14, 10, 0)
    assert next_run_at(s, after=first, timezones=zones) == at(2026, 10, 14, 18, 0)
    # 每个槽位只判断当地正处于 10:00 的账户；同一偏移的时区合并在一次运行里。
    assert [z for z in zones if due_at(s, slot_at=first, timezone=z)] == [
        "Etc/GMT",
        "Africa/Accra",
    ]
    assert due_at(s, slot_at=at(2026, 10, 14, 18, 0), timezone="Etc/GMT+8")
    assert not due_at(s, slot_at=at(2026, 10, 14, 10, 30), timezone="Etc/GMT")


def test_account_periods_follow_local_daytime():
    from app.modules.automation_rules.schedule import due_at

    s = Schedule.model_validate(
        {
            "frequency": "2H",
            "window": "CUSTOM",
            "periods": [{"date_type": "DAY", "start": "08:00", "end": "18:00"}],
            "timezone_basis": "ACCOUNT",
        }
    )
    # 白天时段按各账户当地时间：UTC 06:00 时 UTC 账户当地 06:00、尚未进入时段；
    # UTC 08:00 时 UTC 账户进入时段，UTC-3 账户当地才 05:00，到 UTC 11:00 才判断。
    assert not due_at(s, slot_at=at(2026, 10, 14, 6, 0), timezone="Etc/GMT")
    assert due_at(s, slot_at=at(2026, 10, 14, 8, 0), timezone="Etc/GMT")
    assert not due_at(s, slot_at=at(2026, 10, 14, 8, 0), timezone="Etc/GMT+3")
    assert due_at(s, slot_at=at(2026, 10, 14, 11, 0), timezone="Etc/GMT+3")
