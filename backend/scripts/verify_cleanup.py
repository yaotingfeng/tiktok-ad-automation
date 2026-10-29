# ruff: noqa: T201
"""只读查看过程表统计；估计行数不代表可安全删除行数。"""

from sqlalchemy import text

from app.core.db import engine


def verify_cleanup():
    with engine.connect() as connection:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        connection.execute(text("SET LOCAL statement_timeout = '5s'"))
        rows = connection.execute(
            text("""
            SELECT relname, n_live_tup, n_dead_tup,
                   pg_size_pretty(pg_total_relation_size(relid)) AS total_size,
                   last_autovacuum, last_vacuum
            FROM pg_stat_user_tables
            WHERE relname IN ('report_sync_page', 'pending_dispatch',
                              'report_sync_job', 'ad_directory_run')
            ORDER BY relname
        """)
        )
        for row in rows:
            print(dict(row._mapping))


if __name__ == "__main__":
    verify_cleanup()
