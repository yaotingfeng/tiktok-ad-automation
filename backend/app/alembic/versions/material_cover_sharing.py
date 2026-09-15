"""Separate source cover uploads and fenced IMAGE sharing."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "material_cover_sharing"
down_revision = "material_primary_account"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "material_cover_share_batch",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("bc_id", sa.String(128), nullable=False),
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("source_advertiser_id", sa.String(128), nullable=False),
        sa.Column("source_route", pg.JSONB(), nullable=False),
        sa.Column("target_route", pg.JSONB(), nullable=False),
        sa.Column("members", pg.JSONB(), nullable=False),
        sa.Column("scan_state", pg.JSONB(), nullable=False),
        sa.Column("wake_job_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("armed_at", sa.DateTime(timezone=True)),
        sa.Column("request_digest", sa.String(64)),
        sa.Column("failed_infos", pg.JSONB(), nullable=False),
        sa.Column("request_id", sa.String(255)),
        sa.Column("error_code", sa.String(128)),
        sa.ForeignKeyConstraint(
            ["tenant_id", "bc_id"], ["tenant_bc.tenant_id", "tenant_bc.bc_id"]
        ),
        sa.CheckConstraint(
            "status IN ('PREPARING','SENDING','VERIFYING','UNKNOWN','READY','BLOCKED')",
            name="ck_cover_share_status",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(members) = 'array' AND jsonb_array_length(members) BETWEEN 1 AND 200",
            name="ck_cover_share_members",
        ),
    )
    # 历史任务仍指向原本目标视频，已有发送事实不能改写为新的 SOURCE 请求。
    op.add_column(
        "material_cover_job",
        sa.Column("purpose", sa.String(16), nullable=False, server_default="BUILD"),
    )
    op.alter_column("material_cover_job", "purpose", server_default=None)
    op.add_column("material_cover_job", sa.Column("image_mid", sa.String(128)))
    op.add_column(
        "material_cover_job",
        sa.Column(
            "share_batch_id", sa.Uuid(), sa.ForeignKey("material_cover_share_batch.id")
        ),
    )
    op.create_check_constraint(
        "ck_material_cover_purpose",
        "material_cover_job",
        "purpose IN ('SOURCE','BUILD')",
    )
    op.create_index(
        "ix_cover_share_jobs", "material_cover_job", ["share_batch_id", "id"]
    )
    op.execute("""CREATE FUNCTION protect_cover_share_identity() RETURNS trigger AS $$
    BEGIN
        IF OLD.armed_at IS NOT NULL AND
          ROW(NEW.tenant_id, NEW.bc_id, NEW.actor_id, NEW.source_advertiser_id,
              NEW.source_route, NEW.target_route, NEW.members, NEW.request_digest, NEW.armed_at)
          IS DISTINCT FROM
          ROW(OLD.tenant_id, OLD.bc_id, OLD.actor_id, OLD.source_advertiser_id,
              OLD.source_route, OLD.target_route, OLD.members, OLD.request_digest, OLD.armed_at)
        THEN RAISE EXCEPTION 'Armed IMAGE batch identity is immutable' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
    END; $$ LANGUAGE plpgsql""")
    op.execute("""CREATE TRIGGER cover_share_identity_frozen BEFORE UPDATE ON
        material_cover_share_batch FOR EACH ROW EXECUTE FUNCTION protect_cover_share_identity()""")


def downgrade():
    op.execute(
        "LOCK TABLE material_cover_job, material_cover_share_batch IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM material_cover_share_batch)
            OR EXISTS (SELECT 1 FROM material_cover_job WHERE purpose = 'SOURCE') THEN
            RAISE EXCEPTION 'Cannot drop source cover or IMAGE sharing evidence';
        END IF;
    END $$""")
    op.drop_index("ix_cover_share_jobs", table_name="material_cover_job")
    op.drop_constraint("ck_material_cover_purpose", "material_cover_job", type_="check")
    op.drop_column("material_cover_job", "share_batch_id")
    op.drop_column("material_cover_job", "image_mid")
    op.drop_column("material_cover_job", "purpose")
    op.drop_table("material_cover_share_batch")
    op.execute("DROP FUNCTION protect_cover_share_identity()")
