"""Persist the BC source account without rewriting historical file assignments."""

import sqlalchemy as sa
from alembic import op

revision = "material_primary_account"
down_revision = "preview_display_drama_id"
branch_labels = None
depends_on = None


def upgrade():
    # 历史来源保持不变；首次新分配才从当前合法授权内选择主账户。
    op.add_column(
        "tenant_bc", sa.Column("material_advertiser_id", sa.String(128), nullable=True)
    )


def downgrade():
    op.drop_column("tenant_bc", "material_advertiser_id")
