"""冻结逐广告排除，不改写旧预览正文。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0061_carousel_ad_readiness"
down_revision = "0060_frozen_creative_units"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "preview_ad_readiness",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("preview_id", sa.Uuid(), nullable=False),
        sa.Column("bc_id", sa.String(128), nullable=False),
        sa.Column("ad_id", sa.Uuid(), nullable=False),
        sa.Column("included", sa.Boolean(), nullable=False),
        sa.Column("reason_codes", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "preview_id", "ad_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "preview_id", "bc_id"],
            ["build_preview.tenant_id", "build_preview.id", "build_preview.bc_id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "preview_id", "ad_id"],
            ["planned_ad.tenant_id", "planned_ad.preview_id", "planned_ad.id"],
        ),
        sa.CheckConstraint(
            "included = (jsonb_array_length(reason_codes) = 0)",
            name="ck_preview_ad_readiness",
        ),
    )
    op.execute("""CREATE FUNCTION check_ad_readiness_scope() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NOT EXISTS (SELECT 1 FROM planned_ad a WHERE a.id=NEW.ad_id AND a.tenant_id=NEW.tenant_id AND a.preview_id=NEW.preview_id AND a.bc_id=NEW.bc_id) THEN
        RAISE EXCEPTION 'ad readiness scope mismatch';
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER ad_readiness_scope BEFORE INSERT OR UPDATE ON preview_ad_readiness FOR EACH ROW EXECUTE FUNCTION check_ad_readiness_scope();
    CREATE TRIGGER ad_readiness_frozen BEFORE INSERT OR UPDATE OR DELETE ON preview_ad_readiness FOR EACH ROW EXECUTE FUNCTION check_preview_child_write();
    """)


def downgrade():
    if (
        op.get_bind()
        .execute(sa.text("SELECT EXISTS(SELECT 1 FROM preview_ad_readiness)"))
        .scalar()
    ):
        raise RuntimeError("Cannot discard frozen ad exclusions")
    op.drop_table("preview_ad_readiness")
    op.execute("DROP FUNCTION check_ad_readiness_scope()")
