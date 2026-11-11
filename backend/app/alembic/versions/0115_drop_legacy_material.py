"""物理退休旧素材事实；先保全官方创意身份，再在同一事务移除旧结构。

必须停写、排空并备份；旧表数据不可由 downgrade 重建，回退须恢复经核验的本轮专项恢复包。
先运行 scripts/preserve_legacy_creative_identities.py；upgrade 只验证覆盖，不全量解析。
"""

from alembic import op
from sqlalchemy import text

revision = "0115_drop_legacy_material"
down_revision = "0114_retire_material"
branch_labels = None
depends_on = None


# 先按账户/CID/完整素材关系去重，再与主键做反连接，避免每条历史素材重复索引探测。
# 可直接 EXPLAIN 此冻结 SQL 评估生产预算；不把在线预回填回执当作完整性证据。
IDENTITY_COVERAGE_SQL = """
    SELECT legacy.tenant_id,legacy.advertiser_id,legacy.creative_id
    FROM (
      SELECT DISTINCT m.tenant_id,m.advertiser_id,m.ad_remote_id,m.ad_material_id,
        m.platform_material_id,m.material_type,
        CASE WHEN jsonb_typeof(c.value)='string' THEN c.value #>> '{}' END AS creative_id
      FROM ad_material_reference m
      CROSS JOIN LATERAL jsonb_array_elements(m.creative_ids) c(value)
    ) legacy
    LEFT JOIN ad_creative a ON a.tenant_id=legacy.tenant_id
      AND a.advertiser_id=legacy.advertiser_id AND a.creative_id=legacy.creative_id
    WHERE legacy.creative_id IS NULL OR legacy.creative_id='' OR a.creative_id IS NULL
      OR NOT (
        (a.ad_remote_id=legacy.ad_remote_id
         AND (legacy.ad_material_id IS NULL OR a.ad_material_id IS NOT DISTINCT FROM legacy.ad_material_id)
         AND a.assets @> jsonb_build_array(jsonb_build_object(
           'material_type',legacy.material_type,'platform_material_id',legacy.platform_material_id)))
        OR (a.identity_status='CONFLICT' AND EXISTS (
          SELECT 1 FROM jsonb_array_elements(COALESCE(a.evidence->'conflicts','[]'::jsonb)) e
          WHERE e->'identity'->>'ad_remote_id'=legacy.ad_remote_id
            AND (legacy.ad_material_id IS NULL OR e->'identity'->>'ad_material_id'=legacy.ad_material_id)
            AND e->'identity'->'assets' @> jsonb_build_array(jsonb_build_object(
              'material_type',legacy.material_type,'platform_material_id',legacy.platform_material_id))
        ))
      )
    LIMIT 1
"""


def assert_identity_coverage(connection):
    # CID 是官方标识，不能把非法值或尚未落库的身份随旧列一起丢弃。
    if connection.execute(text(IDENTITY_COVERAGE_SQL)).first() is not None:
        raise RuntimeError(
            "legacy creative identity is not preserved; physical retirement aborted"
        )


def upgrade():
    connection = op.get_bind()
    # 冻结窗口的验证必须有界，超时自动回滚；发布人可预先在线执行同一 SQL 估时。
    previous_lock_timeout = connection.execute(text("SHOW lock_timeout")).scalar_one()
    connection.execute(text("SET LOCAL lock_timeout = '5s'"))
    # 冻结是发布前提；数据库锁再封住验证与删列之间的写入竞态，读请求仍可继续。
    connection.execute(
        text("LOCK TABLE ad_material_reference,ad_creative IN SHARE ROW EXCLUSIVE MODE")
    )
    previous_timeout = connection.execute(text("SHOW statement_timeout")).scalar_one()
    connection.execute(text("SET LOCAL statement_timeout = '300s'"))
    assert_identity_coverage(connection)
    # DROP TABLE 自动移除分区、表索引和触发器；函数需单独移除，禁止 CASCADE
    # 隐藏未知依赖。所有步骤在 Alembic 的同一事务中完成。
    for table in ("material_post_link", "material_report_day", "material_link_scan"):
        op.drop_table(table)
    for table in ("material_post_link", "material_report_day"):
        for operation in ("insert", "update", "delete"):
            op.execute(f"DROP FUNCTION IF EXISTS qrm_{table}_{operation}()")
    op.drop_constraint(
        "ck_ad_material_creatives", "ad_material_reference", type_="check"
    )
    op.drop_column("ad_material_reference", "creative_ids")
    op.execute("""UPDATE ad_sync_bc SET screening_progress = screening_progress ||
      jsonb_build_object('creative_material_physical_retirement',jsonb_build_object(
        'at',clock_timestamp(),'revision','0115_drop_legacy_material'))""")
    connection.execute(
        text("SELECT set_config('statement_timeout',:value,true)"),
        {"value": previous_timeout},
    )
    connection.execute(
        text("SELECT set_config('lock_timeout',:value,true)"),
        {"value": previous_lock_timeout},
    )


def downgrade():
    raise RuntimeError(
        "physical material retirement is irreversible; restore the reviewed retirement recovery bundle"
    )
