"""规则运行时刻：在指定时区按墙钟对齐频率槽位，再筛选生效时段。

按规则时区运行时只有一个时区；按账户时区运行时，规则范围内每个账户时区各自产生槽位，
下一次运行取最早的槽位，运行时只判断当地正处于该槽位的账户（见 ``due_at``）。
"""

from collections.abc import Iterable
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


def _active(schedule: Schedule, day: date, slot: time) -> bool:
    return schedule.window == "CONTINUOUS" or any(
        _in_period(day, slot, period) for period in schedule.periods
    )


def _next_in_zone(
    schedule: Schedule, slots: list[time], after: datetime, zone: ZoneInfo
) -> datetime | None:
    start_day = after.astimezone(zone).date()
    for offset in range(_HORIZON_DAYS + 1):
        day = start_day + timedelta(days=offset)
        for slot in slots:
            candidate = datetime.combine(day, slot, zone)
            if candidate > after and _active(schedule, day, slot):
                return candidate.astimezone(UTC)
    return None


def next_run_at(
    schedule: Schedule, *, after: datetime, timezones: Iterable[str]
) -> datetime | None:
    """严格晚于 after 的下一次运行时刻（UTC）；多个时区取最早的槽位，没有时返回 None。"""
    if schedule.window == "ONCE":
        assert schedule.once_at is not None
        return schedule.once_at.astimezone(UTC) if schedule.once_at > after else None
    slots = _slots(schedule)
    found = (
        _next_in_zone(schedule, slots, after, ZoneInfo(zone))
        for zone in sorted(set(timezones))
    )
    return min((moment for moment in found if moment is not None), default=None)


def due_at(schedule: Schedule, *, slot_at: datetime, timezone: str) -> bool:
    """该时区的当地时间在 slot_at 时是否正好是一个生效槽位；按账户时区运行时据此挑选账户。"""
    local = slot_at.astimezone(ZoneInfo(timezone))
    if local.second or local.microsecond:
        return False
    moment = local.time().replace(tzinfo=None)
    return moment in _slots(schedule) and _active(schedule, local.date(), moment)
