"""Cancel stacked periodic directory runs left by the dispatch backlog."""

from alembic import op

revision = "0031_cancel_stacked_dir_runs"
down_revision = "0030_actor_user_reference"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 投递积压期间每 3 小时的周期目录请求互不合并，同一账户同一查询叠加了多条
    # 尚未开始的运行；周期“只读在投对象”运行也已被全量目录取代。只取消尚未被
    # 领取的排队运行：每组保留最新一条，执行时读到的就是最新数据。已发布的旧
    # 消息执行时看到 CANCELLED 直接结束，不调用平台接口。
    op.execute("""
    UPDATE ad_directory_run SET status = 'CANCELLED', error_code = 'superseded',
           completed_at = now()
    WHERE status = 'QUEUED' AND claim_token IS NULL
      AND query->>'include_deleted' = 'false'
      AND query->'ids' = '[]'::jsonb AND query->'parent_ids' = '[]'::jsonb
    """)
    op.execute("""
    UPDATE ad_directory_run r SET status = 'CANCELLED', error_code = 'superseded',
           completed_at = now()
    FROM (
        SELECT id, row_number() OVER (
            PARTITION BY tenant_id, advertiser_id, partition_key
            ORDER BY request_sequence DESC
        ) AS rn
        FROM ad_directory_run
        WHERE status = 'QUEUED' AND claim_token IS NULL
    ) ranked
    WHERE r.id = ranked.id AND ranked.rn > 1
    """)


def downgrade() -> None:
    # 取消的是可由下一轮周期请求重新生成的排队运行，不恢复。
    pass
