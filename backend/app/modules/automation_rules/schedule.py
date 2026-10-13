"""规则运行时刻：在租户时区按墙钟对齐频率槽位，再筛选生效时段。"""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.modules.automation_rules.schemas import FREQUENCY_MINUTES, Period, Schedule

# 自定义时段最多按月循环，向后找两个月仍无槽位即视为没有下一次运行。
_HORIZON_DAYS = 62


def _in_period(day: date, moment: time, period: Period) -> bool:
    if period.date_type == "WEEK" and day.isoweekday() not in period.days:
        return False
    if period.date_type == "MONTH" and day.day not in period.days:
        return False
    return period.start <= moment <= period.end


def _slots(schedule: Schedule) -> list[time]:
    """一天内的运行槽位：每日固定时刻，或从当地零点起按频率等分。"""
    if schedule.frequency == "DAILY":
        assert schedule.daily_at is not None
        return [schedule.daily_at.replace(second=0, microsecond=0)]
    minutes = FREQUENCY_MINUTES[schedule.frequency]
    return [time(hour=m // 60, minute=m % 60) for m in range(0, 24 * 60, minutes)]


def next_run_at(
    schedule: Schedule, *, after: datetime, timezone: str
) -> datetime | None:
    """严格晚于 after 的下一次运行时刻（UTC）；没有时返回 None。"""
    if schedule.window == "ONCE":
        assert schedule.once_at is not None
        return schedule.once_at.astimezone(UTC) if schedule.once_at > after else None
    zone = ZoneInfo(timezone)
    start_day = after.astimezone(zone).date()
    slots = _slots(schedule)
    for offset in range(_HORIZON_DAYS + 1):
        day = start_day + timedelta(days=offset)
        for slot in slots:
            candidate = datetime.combine(day, slot, zone)
            if candidate <= after:
                continue
            if schedule.window == "CONTINUOUS" or any(
                _in_period(day, slot, period) for period in schedule.periods
            ):
                return candidate.astimezone(UTC)
    return None
