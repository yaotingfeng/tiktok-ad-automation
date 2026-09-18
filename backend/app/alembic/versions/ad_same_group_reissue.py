"""允许明确批准的 UNKNOWN 广告在原广告组内单条补建。"""

from alembic import op

revision = "ad_same_group_reissue"
down_revision = "cover_claim_reissue"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint(
        "ck_replacement_remote_ids",
        "build_verified_replacement",
        type_="check",
    )
    op.create_check_constraint(
        "ck_replacement_remote_ids",
        "build_verified_replacement",
        "length(trim(remote_ad_id))>0 AND length(trim(remote_adgroup_id))>0 AND length(trim(original_adgroup_id))>0",
    )


def downgrade():
    op.execute("""
        DO $$ BEGIN
            IF EXISTS(
                SELECT 1 FROM build_verified_replacement
                WHERE remote_adgroup_id = original_adgroup_id
            ) THEN
                RAISE EXCEPTION 'cannot downgrade retained same-group ad reissue evidence';
            END IF;
        END $$;
    """)
    op.drop_constraint(
        "ck_replacement_remote_ids",
        "build_verified_replacement",
        type_="check",
    )
    op.create_check_constraint(
        "ck_replacement_remote_ids",
        "build_verified_replacement",
        "length(trim(remote_ad_id))>0 AND length(trim(remote_adgroup_id))>0 AND length(trim(original_adgroup_id))>0 AND remote_adgroup_id<>original_adgroup_id",
    )
