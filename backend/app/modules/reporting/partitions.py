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
    ensure_month_partitions_for_range(
        session, _month_start(today, 0), _month_start(today, months_ahead)
    )


def ensure_month_partitions_for_range(
    session: Session,
    start: date,
    end: date,
    *,
    tables: tuple[str, ...] = PARTITIONED_TABLES,
) -> None:
    """原子迁移 DEFAULT 既有行；发布只传本表，预建才准备全部事实表。"""
    if not tables or any(table not in PARTITIONED_TABLES for table in tables):
        raise ValueError("unknown reporting partition table")
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        nxt = _month_start(cursor, 1)
        for table in tables:
            name = f"{table}_{cursor:%Y_%m}"
            connection = session.connection()
            if connection.execute(
                text("SELECT to_regclass(:name)"), {"name": name}
            ).scalar():
                continue
            # 只锁本次发布的父表，避免持广告表锁再等素材表，与关联读形成锁环。
            # 锁后复查处理同月并发创建；搬迁、挂载和事实发布仍由调用方统一提交。
            connection.execute(text(f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE"))
            if connection.execute(
                text("SELECT to_regclass(:name)"), {"name": name}
            ).scalar():
                continue
            connection.execute(
                text(
                    f"CREATE TABLE {name} (LIKE {table} INCLUDING DEFAULTS INCLUDING CONSTRAINTS)"
                )
            )
            connection.execute(
                text(
                    f"WITH moved AS (DELETE FROM {table}_default WHERE stat_date >= :s AND stat_date < :e RETURNING *) "
                    f"INSERT INTO {name} SELECT * FROM moved"
                ),
                {"s": cursor, "e": nxt},
            )
            connection.execute(
                text(
                    f"ALTER TABLE {table} ATTACH PARTITION {name} "
                    f"FOR VALUES FROM ('{cursor.isoformat()}') TO ('{nxt.isoformat()}')"
                )
            )
        cursor = nxt
