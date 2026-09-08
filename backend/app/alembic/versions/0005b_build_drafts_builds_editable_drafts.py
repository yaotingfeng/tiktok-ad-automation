"""builds editable drafts

Revision ID: 0005b_build_drafts
Revises: 0005_strategies
Create Date: 2026-09-09 05:37:24.534768

"""

from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0005b_build_drafts"
down_revision = "0005_strategies"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "build_draft",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "bc_id", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("strategy_version_id", sa.Uuid(), nullable=False),
        sa.Column("provider_connection_id", sa.Uuid(), nullable=False),
        sa.Column(
            "application_id",
            sqlmodel.sql.sqltypes.AutoString(length=255),
            nullable=False,
        ),
        sa.Column(
            "link_config", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column(
            "request_digest",
            sqlmodel.sql.sqltypes.AutoString(length=64),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('DRAFT','PREPARING','READY','BLOCKED')",
            name="ck_build_draft_status",
        ),
        sa.CheckConstraint("revision > 0", name="ck_build_draft_revision"),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["user.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "bc_id"],
            ["tenant_bc.tenant_id", "tenant_bc.bc_id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "provider_connection_id", "application_id"],
            [
                "provider_application.tenant_id",
                "provider_application.connection_id",
                "provider_application.external_id",
            ],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "strategy_version_id"],
            ["strategy_version.tenant_id", "strategy_version.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id", "bc_id", name="uq_build_draft_bc"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_build_draft_tenant_id"),
        sa.UniqueConstraint("tenant_id", "request_id", name="uq_build_draft_request"),
    )
    op.create_index(
        "ix_build_draft_tenant_page",
        "build_draft",
        ["tenant_id", "bc_id", "created_at", "id"],
        unique=False,
    )
    op.create_table(
        "draft_preparation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("draft_id", sa.Uuid(), nullable=False),
        sa.Column("draft_revision", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("provider_task_id", sa.Uuid(), nullable=True),
        sa.Column("phase", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("account_after", sa.Integer(), nullable=False),
        sa.Column("link_cursor", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("pending_links", sa.Boolean(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("dispatch_id", sa.Uuid(), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("repair_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error_code", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.CheckConstraint(
            "phase IN ('accounts','links','materials','done')",
            name="ck_draft_preparation_phase",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','READY','BLOCKED','OBSOLETE')",
            name="ck_draft_preparation_status",
        ),
        sa.CheckConstraint(
            "draft_revision > 0 AND generation >= 0 AND account_after >= 0",
            name="ck_draft_preparation_progress",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["user.id"],
        ),
        sa.ForeignKeyConstraint(
            ["dispatch_id"],
            ["pending_dispatch.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "draft_id"],
            ["build_draft.tenant_id", "build_draft.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "provider_task_id"],
            ["link_preparation.tenant_id", "link_preparation.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "draft_id",
            "draft_revision",
            name="uq_draft_preparation_revision",
        ),
        sa.UniqueConstraint(
            "tenant_id", "draft_id", "id", name="uq_draft_preparation_identity"
        ),
        sa.UniqueConstraint(
            "tenant_id", "request_id", name="uq_draft_preparation_request"
        ),
    )
    op.create_index(
        "ix_draft_preparation_repair",
        "draft_preparation",
        ["status", "repair_after", "id"],
        unique=False,
    )
    op.create_table(
        "draft_account",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("draft_id", sa.Uuid(), nullable=False),
        sa.Column(
            "advertiser_id",
            sqlmodel.sql.sqltypes.AutoString(length=128),
            nullable=False,
        ),
        sa.Column(
            "bc_id", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False
        ),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column(
            "currency", sqlmodel.sql.sqltypes.AutoString(length=8), nullable=False
        ),
        sa.Column(
            "timezone", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False
        ),
        sa.Column("first_line", sa.Integer(), nullable=False),
        sa.CheckConstraint("first_line > 0", name="ck_draft_account_line"),
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
            ["tenant_id", "draft_id", "bc_id"],
            ["build_draft.tenant_id", "build_draft.id", "build_draft.bc_id"],
        ),
        sa.PrimaryKeyConstraint("tenant_id", "draft_id", "advertiser_id"),
    )
    op.create_table(
        "draft_drama",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("draft_id", sa.Uuid(), nullable=False),
        sa.Column("drama_id", sa.Uuid(), nullable=False),
        sa.Column(
            "bc_id", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False
        ),
        sa.Column("link_id", sa.Uuid(), nullable=False),
        sa.Column(
            "title", sqlmodel.sql.sqltypes.AutoString(length=1000), nullable=False
        ),
        sa.Column("first_line", sa.Integer(), nullable=False),
        sa.Column("material_state", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("material_cursor", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("matched_count", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "material_state IN ('pending','matching','ready','manual')",
            name="ck_draft_drama_material_state",
        ),
        sa.CheckConstraint(
            "first_line > 0 AND matched_count >= 0", name="ck_draft_drama_progress"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "draft_id", "bc_id"],
            ["build_draft.tenant_id", "build_draft.id", "build_draft.bc_id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "drama_id"],
            ["provider_drama.tenant_id", "provider_drama.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "link_id"],
            ["promotion_link.tenant_id", "promotion_link.id"],
        ),
        sa.PrimaryKeyConstraint("tenant_id", "draft_id", "drama_id"),
        sa.UniqueConstraint(
            "tenant_id", "draft_id", "bc_id", "drama_id", name="uq_draft_drama_bc"
        ),
    )
    op.create_table(
        "draft_input",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("draft_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sqlmodel.sql.sqltypes.AutoString(length=16), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column(
            "raw_text", sqlmodel.sql.sqltypes.AutoString(length=1000), nullable=False
        ),
        sa.Column(
            "status", sqlmodel.sql.sqltypes.AutoString(length=32), nullable=False
        ),
        sa.Column("reason_code", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("duplicate_of", sa.Integer(), nullable=True),
        sa.Column("advertiser_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("drama_id", sa.Uuid(), nullable=True),
        sa.Column("provider_input_id", sa.Uuid(), nullable=True),
        sa.Column(
            "candidates", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.CheckConstraint(
            "kind IN ('drama','account') AND line_no > 0", name="ck_draft_input_line"
        ),
        sa.CheckConstraint(
            "duplicate_of IS NULL OR duplicate_of > 0", name="ck_draft_input_duplicate"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "draft_id"],
            ["build_draft.tenant_id", "build_draft.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "provider_input_id"],
            ["link_preparation_item.tenant_id", "link_preparation_item.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "draft_id", "kind", "line_no", name="uq_draft_input_line"
        ),
    )
    op.create_table(
        "draft_preparation_request",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("draft_id", sa.Uuid(), nullable=False),
        sa.Column("preparation_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "draft_id", "preparation_id"],
            [
                "draft_preparation.tenant_id",
                "draft_preparation.draft_id",
                "draft_preparation.id",
            ],
        ),
        sa.PrimaryKeyConstraint("tenant_id", "request_id"),
    )
    op.create_table(
        "draft_group_material",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("draft_id", sa.Uuid(), nullable=False),
        sa.Column("drama_id", sa.Uuid(), nullable=False),
        sa.Column("group_no", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "bc_id", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False
        ),
        sa.Column("material_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "group_no > 0 AND position > 0", name="ck_draft_group_position"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "bc_id", "material_id"],
            ["material_file.tenant_id", "material_file.bc_id", "material_file.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "draft_id", "bc_id", "drama_id"],
            [
                "draft_drama.tenant_id",
                "draft_drama.draft_id",
                "draft_drama.bc_id",
                "draft_drama.drama_id",
            ],
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id", "draft_id", "drama_id", "group_no", "position"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "draft_id",
            "drama_id",
            "material_id",
            name="uq_draft_drama_material",
        ),
    )
    op.create_index(
        "ix_draft_shared_material",
        "draft_group_material",
        ["tenant_id", "draft_id", "material_id"],
        unique=False,
    )


def downgrade():
    op.drop_index("ix_draft_shared_material", table_name="draft_group_material")
    op.drop_table("draft_group_material")
    op.drop_table("draft_preparation_request")
    op.drop_table("draft_input")
    op.drop_table("draft_drama")
    op.drop_table("draft_account")
    op.drop_index("ix_draft_preparation_repair", table_name="draft_preparation")
    op.drop_table("draft_preparation")
    op.drop_index("ix_build_draft_tenant_page", table_name="build_draft")
    op.drop_table("build_draft")
