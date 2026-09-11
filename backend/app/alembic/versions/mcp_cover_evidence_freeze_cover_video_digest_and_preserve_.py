"""Freeze the original cover video digest and preserve actual image receipt facts.

Revision ID: mcp_cover_evidence
Revises: mcp_draft_connection
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "mcp_cover_evidence"
down_revision = "mcp_draft_connection"
branch_labels = None
depends_on = None


def upgrade():
    # 历史 NULL 保持缺证据，不从今日素材或详情回填原请求的事实。
    op.add_column(
        "material_cover_job", sa.Column("video_md5", sa.String(32), nullable=True)
    )
    op.create_check_constraint(
        "ck_material_cover_video_md5",
        "material_cover_job",
        "video_md5 IS NULL OR video_md5 ~ '^[0-9a-f]{32}$'",
    )
    op.add_column(
        "material_cover_receipt",
        sa.Column("receipt_facts", postgresql.JSONB(none_as_null=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_material_cover_receipt_facts",
        "material_cover_receipt",
        "receipt_facts IS NULL OR (jsonb_typeof(receipt_facts) = 'object' "
        "AND receipt_facts ? 'signature' "
        "AND receipt_facts - 'signature' = '{}'::jsonb "
        "AND (receipt_facts->'signature' = 'null'::jsonb OR "
        "(jsonb_typeof(receipt_facts->'signature') = 'string' "
        "AND receipt_facts->>'signature' ~ '^[0-9a-f]{32}$')))",
    )
    op.execute("""
        CREATE FUNCTION mcp_cover_digest_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.video_md5 IS DISTINCT FROM OLD.video_md5 THEN
                RAISE EXCEPTION 'immutable cover video digest' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER material_cover_digest_guard
        BEFORE UPDATE ON material_cover_job
        FOR EACH ROW EXECUTE FUNCTION mcp_cover_digest_guard()
    """)
    op.execute("""
        CREATE FUNCTION mcp_cover_receipt_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW IS DISTINCT FROM OLD THEN
                RAISE EXCEPTION 'immutable cover receipt' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER material_cover_receipt_guard
        BEFORE UPDATE ON material_cover_receipt
        FOR EACH ROW EXECUTE FUNCTION mcp_cover_receipt_guard()
    """)


def downgrade():
    # 两表先锁再查；新证据存在时拒绝丢弃，保留未知上传的恢复依据。
    op.execute(
        "LOCK TABLE material_cover_job, material_cover_receipt IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM material_cover_job WHERE video_md5 IS NOT NULL)
               OR EXISTS (SELECT 1 FROM material_cover_receipt WHERE receipt_facts IS NOT NULL) THEN
                RAISE EXCEPTION 'Cannot drop frozen cover evidence';
            END IF;
        END $$
    """)
    op.execute("DROP TRIGGER material_cover_receipt_guard ON material_cover_receipt")
    op.execute("DROP FUNCTION mcp_cover_receipt_guard()")
    op.execute("DROP TRIGGER material_cover_digest_guard ON material_cover_job")
    op.execute("DROP FUNCTION mcp_cover_digest_guard()")
    op.drop_constraint(
        "ck_material_cover_receipt_facts", "material_cover_receipt", type_="check"
    )
    op.drop_column("material_cover_receipt", "receipt_facts")
    op.drop_constraint(
        "ck_material_cover_video_md5", "material_cover_job", type_="check"
    )
    op.drop_column("material_cover_job", "video_md5")
