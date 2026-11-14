"""Preserve authenticated producer lineage on immutable pushed material revision."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0128_material_production_origin"
down_revision = "0127_ai_knowledge"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "material_push_item",
        sa.Column("production_origin", postgresql.JSONB(), nullable=True),
    )


def downgrade():
    op.drop_column("material_push_item", "production_origin")
