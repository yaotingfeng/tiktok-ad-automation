#!/usr/bin/env python3
"""
清理 ad_directory_run 表的历史数据

问题：163万行已完成的历史记录，占用 3GB 空间，拖慢所有查询
解决：分批删除 30 天前已完成的记录
"""

import os
import sys
from datetime import datetime, timedelta, UTC

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from sqlalchemy import create_engine, text
from app.core.config import settings

BATCH_SIZE = 10000  # 每批删除数量
RETENTION_DAYS = 30  # 保留最近 N 天的数据
DRY_RUN = True  # 先预览，不实际删除


def main():
    engine = create_engine(str(settings.DATABASE_URL))
    cutoff_date = datetime.now(UTC) - timedelta(days=RETENTION_DAYS)

    with engine.connect() as conn:
        # 1. 统计需要清理的数据
        result = conn.execute(text("""
            SELECT
                COUNT(*) as count,
                pg_size_pretty(SUM(pg_column_size(ad_directory_run.*))) as estimated_size
            FROM ad_directory_run
            WHERE status = 'COMPLETE'
            AND completed_at < :cutoff_date
        """), {"cutoff_date": cutoff_date})

        row = result.fetchone()
        total_to_delete = row.count
        estimated_size = row.estimated_size

        print(f"=== 清理计划 ===")
        print(f"保留策略: 最近 {RETENTION_DAYS} 天")
        print(f"截止日期: {cutoff_date}")
        print(f"需要删除: {total_to_delete:,} 行")
        print(f"预计释放: {estimated_size}")
        print()

        if total_to_delete == 0:
            print("✓ 无需清理")
            return

        if DRY_RUN:
            print("⚠️  DRY RUN 模式，不会实际删除")
            print("确认无误后，设置 DRY_RUN = False 再运行")
            return

        # 2. 分批删除
        print("=== 开始清理 ===")
        total_deleted = 0
        batch = 0

        while True:
            batch += 1
            result = conn.execute(text("""
                DELETE FROM ad_directory_run
                WHERE id IN (
                    SELECT id FROM ad_directory_run
                    WHERE status = 'COMPLETE'
                    AND completed_at < :cutoff_date
                    LIMIT :batch_size
                )
                RETURNING id
            """), {"cutoff_date": cutoff_date, "batch_size": BATCH_SIZE})

            deleted_count = len(list(result))
            total_deleted += deleted_count

            print(f"Batch {batch}: 删除了 {deleted_count} 行 (总计: {total_deleted:,}/{total_to_delete:,})")

            if deleted_count == 0:
                break

            conn.commit()

        print()
        print(f"✓ 清理完成，共删除 {total_deleted:,} 行")
        print()
        print("=== 建议后续操作 ===")
        print("1. 运行 VACUUM ANALYZE ad_directory_run; -- 回收空间和更新统计")
        print("2. 添加定时任务每天自动清理")


if __name__ == "__main__":
    main()
