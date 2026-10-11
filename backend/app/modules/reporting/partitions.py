# backend/app/modules/reporting/partitions.py
"""按月分区：大表（广告日、素材日）每月一个分区，另有 DEFAULT 兜底。"""

from datetime import date

from sqlalchemy import text
from sqlmodel import Session

PARTITIONED_TABLES = ("ad_report_day", "material_report_day")


def _month_start(value: date, offset: int) -> date:
    month = value.month - 1 + offset
    return date(value.year + month // 12, month % 12 + 1, 1)


def ensure_month_partitions(
    session: Session, *, months_ahead: int = 3, today: date | None = None
) -> None:
    """创建当月及未来 months_ahead 个月的分区；已存在则跳过，可重复调用。"""
    today = today or date.today()
    for table in PARTITIONED_TABLES:
        for offset in range(0, months_ahead + 1):
            start, end = _month_start(today, offset), _month_start(today, offset + 1)
            name = f"{table}_{start:%Y_%m}"
            session.execute(
                text(
                    f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF {table} "
                    f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
                )
            )


def ensure_month_partitions_for_range(session: Session, start: date, end: date) -> None:
    """为历史区间逐月建分区，必须在写入该区间数据之前调用。"""
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        nxt = _month_start(cursor, 1)
        for table in PARTITIONED_TABLES:
            session.execute(
                text(
                    f"CREATE TABLE IF NOT EXISTS {table}_{cursor:%Y_%m} PARTITION OF {table} "
                    f"FOR VALUES FROM ('{cursor.isoformat()}') TO ('{nxt.isoformat()}')"
                )
            )
        cursor = nxt
