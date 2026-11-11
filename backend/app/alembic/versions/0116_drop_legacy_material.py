"""第一阶段物理退休三张旧素材表；保留旧 CID 列供在线保全。

必须停写、排空并备份；旧表数据不可由 downgrade 重建，回退须恢复经核验的本轮专项恢复包。
本阶段不运行身份覆盖守卫、不删除 creative_ids；新行默认空数组，既有列值不变。
旧关系完整保全与守卫通过后，另行发布删列迁移。
"""

from alembic import op
from sqlalchemy import text

revision = "0116_drop_legacy_material"
down_revision = "0115_copy_current_source"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    # 不等待长查询无限占锁；三表 DDL、默认值和退休回执在同一事务中提交。
    previous_lock_timeout = connection.execute(text("SHOW lock_timeout")).scalar_one()
    connection.execute(text("SET LOCAL lock_timeout = '5s'"))
    # DROP TABLE 自动移除分区、表索引和触发器；函数需单独移除，禁止 CASCADE
    # 隐藏未知依赖。所有步骤在 Alembic 的同一事务中完成。
    for table in ("material_post_link", "material_report_day", "material_link_scan"):
        op.drop_table(table)
    for table in ("material_post_link", "material_report_day"):
        for operation in ("insert", "update", "delete"):
            op.execute(f"DROP FUNCTION IF EXISTS qrm_{table}_{operation}()")
    # 新程序不再写旧列；默认空数组只服务新行，不回写或抹掉尚待保全的旧 CID。
    op.execute(
        "ALTER TABLE ad_material_reference ALTER COLUMN creative_ids SET DEFAULT '[]'::jsonb"
    )
    op.execute("""UPDATE ad_sync_bc SET screening_progress = screening_progress ||
      jsonb_build_object('creative_material_table_retirement',jsonb_build_object(
        'at',clock_timestamp(),'revision','0116_drop_legacy_material'))""")
    connection.execute(
        text("SELECT set_config('lock_timeout',:value,true)"),
        {"value": previous_lock_timeout},
    )


def downgrade():
    raise RuntimeError(
        "physical material retirement is irreversible; restore the reviewed retirement recovery bundle"
    )
