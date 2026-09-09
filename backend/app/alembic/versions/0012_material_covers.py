"""Persist fenced target-video cover uploads and known-ID receipts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012_material_covers"
down_revision = "0011_recovery_evidence_index"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "material_cover_job",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("bc_id", sa.String(128), nullable=False),
        sa.Column("material_id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("advertiser_id", sa.String(128), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("video_id", sa.String(255), nullable=False),
        sa.Column("remote_name", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("request_armed_at", sa.DateTime(timezone=True)),
        sa.Column("known_image_id", sa.String(255)),
        sa.Column("signature", sa.String(128)),
        sa.Column("width", sa.Integer()),
        sa.Column("height", sa.Integer()),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("claim_token", sa.Uuid()),
        sa.Column("claimed_until", sa.DateTime(timezone=True)),
        sa.Column("dispatch_id", sa.Uuid(), sa.ForeignKey("pending_dispatch.id")),
        sa.Column("next_page", sa.Integer(), nullable=False),
        sa.Column("search_round", sa.Uuid(), nullable=False),
        sa.Column("search_total", sa.Integer()),
        sa.Column("candidate_image_id", sa.String(255)),
        sa.Column("search_ambiguous", sa.Boolean(), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("repair_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "bc_id", "material_id"],
            ["material_file.tenant_id", "material_file.bc_id", "material_file.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "bc_id", "advertiser_id", "connection_id"],
            [
                "bc_account_access.tenant_id",
                "bc_account_access.bc_id",
                "bc_account_access.advertiser_id",
                "bc_account_access.connection_id",
            ],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "bc_id", "material_id", "asset_id"],
            [
                "account_material.tenant_id",
                "account_material.bc_id",
                "account_material.material_id",
                "account_material.id",
            ],
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_material_cover_scope"),
        sa.UniqueConstraint(
            "tenant_id",
            "asset_id",
            "connection_id",
            "video_id",
            name="uq_material_cover_video",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','PREPARING','VERIFYING','READY','UNKNOWN','BLOCKED')",
            name="ck_material_cover_status",
        ),
        sa.CheckConstraint(
            "revision >= 0 AND next_page > 0 AND failure_count >= 0",
            name="ck_material_cover_counters",
        ),
        sa.CheckConstraint(
            "(claim_token IS NULL) = (claimed_until IS NULL)",
            name="ck_material_cover_claim",
        ),
        sa.CheckConstraint(
            "length(trim(video_id)) > 0 AND length(trim(remote_name)) > 0",
            name="ck_material_cover_identity",
        ),
        sa.CheckConstraint(
            "known_image_id IS NULL OR request_armed_at IS NOT NULL",
            name="ck_material_cover_receipt",
        ),
    )
    op.create_index(
        "ix_material_cover_repair",
        "material_cover_job",
        ["status", "repair_after", "id"],
    )
    op.create_table(
        "material_cover_receipt",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("image_id", sa.String(255), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "job_id"],
            ["material_cover_job.tenant_id", "material_cover_job.id"],
        ),
        sa.UniqueConstraint(
            "job_id", "image_id", name="uq_material_cover_receipt_image"
        ),
    )
    op.create_table(
        "material_cover_job_page",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("search_round", sa.Uuid(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("image_ids", postgresql.JSONB(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "job_id"],
            ["material_cover_job.tenant_id", "material_cover_job.id"],
        ),
        sa.UniqueConstraint(
            "job_id", "search_round", "page", name="uq_material_cover_search_page"
        ),
        sa.CheckConstraint(
            "page > 0 AND total >= 0 AND jsonb_array_length(image_ids) <= 100",
            name="ck_material_cover_page_size",
        ),
    )
    op.add_column("execution_step", sa.Column("cover_job_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_execution_step_cover_job",
        "execution_step",
        "material_cover_job",
        ["cover_job_id"],
        ["id"],
    )


def downgrade():
    op.drop_constraint(
        "fk_execution_step_cover_job", "execution_step", type_="foreignkey"
    )
    op.drop_column("execution_step", "cover_job_id")
    op.drop_table("material_cover_job_page")
    op.drop_table("material_cover_receipt")
    op.drop_index("ix_material_cover_repair", table_name="material_cover_job")
    op.drop_table("material_cover_job")
