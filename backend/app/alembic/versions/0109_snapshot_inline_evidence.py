"""Freeze material version identities inline, protected by snapshot retention."""

from alembic import op

revision = "0109_snapshot_inline_evidence"
down_revision = "0108_report_lookup_cover"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 常量默认值为元数据变更，不重写快照或历史证据；旧快照仍保留原来的外键保护。
    op.execute("""ALTER TABLE query_snapshot_row
        ADD COLUMN evidence_versions uuid[] NOT NULL DEFAULT '{}'::uuid[]""")
    op.execute("""UPDATE query_snapshot_row q SET evidence_versions=l.versions
        FROM (SELECT snapshot_id,row_key,array_agg(version_id ORDER BY version_id) versions
            FROM query_snapshot_evidence GROUP BY snapshot_id,row_key) l
        WHERE q.snapshot_id=l.snapshot_id AND q.row_key=l.row_key""")
    op.execute("""CREATE INDEX ix_read_bucket_projection_period
        ON read_bucket_state(tenant_id,kind,bucket,advertiser_id)
        INCLUDE(source_revision,derived_revision,derived_observed_at,derived_status)""")
    op.execute("""CREATE INDEX ix_material_post_link_post
        ON material_post_link(tenant_id,main_material_id,advertiser_id,main_material_type)
        INCLUDE(ad_remote_id,ad_material_id) WHERE ad_material_id IS NOT NULL""")


def downgrade() -> None:
    op.execute("DROP INDEX ix_material_post_link_post")
    op.execute("DROP INDEX ix_read_bucket_projection_period")
    # 过期快照允许已按保留合同清理版本；不尝试重建这些无效结果的外键。
    op.execute("DELETE FROM query_snapshot WHERE expires_at<=clock_timestamp()")
    op.execute("""INSERT INTO query_snapshot_evidence(snapshot_id,row_key,version_id)
        SELECT q.snapshot_id,q.row_key,v.id FROM query_snapshot_row q
        CROSS JOIN LATERAL unnest(q.evidence_versions) v(id)
        ON CONFLICT DO NOTHING""")
    op.execute("ALTER TABLE query_snapshot_row DROP COLUMN evidence_versions")
