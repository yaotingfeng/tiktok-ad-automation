"""批量共享先按锚点账户取有限素材，避免万级候选扫描。"""

from alembic import op

revision = "material_batch_candidate_index"
down_revision = "ad_same_group_reissue"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_material_distribution_batch_target",
        "material_distribution",
        ["tenant_id", "bc_id", "actor_id", "advertiser_id", "material_id"],
        postgresql_where="status = 'queued' AND superseded_by_id IS NULL",
    )


def downgrade():
    op.drop_index(
        "ix_material_distribution_batch_target",
        table_name="material_distribution",
    )
