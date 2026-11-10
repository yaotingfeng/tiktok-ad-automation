"""退休旧素材任务，保留原始事实至少七天，发布期间必须停写排空。"""

from alembic import op
from sqlalchemy import text

revision = "0114_retire_material"
down_revision = "0113_creative_material"
branch_labels = None
depends_on = None


def retire(connection):
    # 旧需求保留未满足证据，不能把未读到的数据伪装为成功；epoch使旧冻结引用失效。
    connection.execute(
        text("""
      UPDATE sync_scope SET owner_epoch=owner_epoch+1
      WHERE resource IN ('material_day','material_links')
    """)
    )
    connection.execute(
        text("""
      UPDATE sync_schedule h SET active=false,generation=generation+1,lease_token=NULL,
        leased_until=NULL,lease_execution_id=NULL,lease_execution_generation=NULL
      FROM sync_scope s WHERE s.id=h.scope_id AND s.resource IN ('material_day','material_links')
    """)
    )
    connection.execute(
        text("""
      UPDATE sync_execution_scope e SET status='SUPERSEDED'
      FROM sync_scope s WHERE s.id=e.scope_id AND s.resource IN ('material_day','material_links') AND e.status='PENDING'
    """)
    )
    connection.execute(
        text("""
      UPDATE report_sync_job SET status='CANCELLED',error_code='report_kind_retired',
        finished_at=clock_timestamp(),claim_generation=claim_generation+1,claim_token=NULL,claimed_until=NULL
      WHERE kind IN ('material_day','material_links') AND status IN ('QUEUED','RUNNING','WAITING')
    """)
    )
    # 未知尾部仍由已有保守结算器处理；只使失效执行到期，不伪造发送/退款收据。
    connection.execute(
        text("""
      UPDATE sync_cost_ledger l SET lease_until=LEAST(l.lease_until,clock_timestamp())
      FROM report_sync_job j WHERE j.id=l.report_job_id AND j.kind IN ('material_day','material_links') AND NOT l.closed
    """)
    )
    connection.execute(
        text("""
      UPDATE pending_dispatch d SET superseded_at=COALESCE(d.superseded_at,clock_timestamp())
      FROM report_sync_job j WHERE d.tenant_id=j.tenant_id AND d.payload->>'job_id'=j.id::text
        AND d.task_name IN ('reporting.job_step','reporting.backfill_step')
        AND j.kind IN ('material_day','material_links') AND d.published_at IS NULL
    """)
    )
    connection.execute(
        text("""
      UPDATE ad_sync_bc SET screening_progress=screening_progress ||
        jsonb_build_object('creative_material_cutover',jsonb_build_object('at',clock_timestamp(),'retained_tables',
        jsonb_build_array('material_report_day','material_post_link','material_link_scan'),'minimum_retention_days',7))
    """)
    )


def upgrade():
    retire(op.get_bind())


def downgrade():
    raise RuntimeError(
        "Creative cutover requires reviewed forward recovery; cancelled legacy work must not be revived automatically"
    )
