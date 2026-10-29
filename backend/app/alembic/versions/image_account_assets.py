"""Typed primary image mappings; preserve video cover semantics.

Revision ID: 0047_image_account_assets
Revises: 0046_image_materials
"""

import sqlalchemy as sa
from alembic import op

revision = "0047_image_account_assets"
down_revision = "0046_image_materials"
branch_labels = None
depends_on = None


VIDEO_ID_NONEMPTY = "length(translate(video_id, chr(9) || chr(10) || chr(11) || chr(12) || chr(13) || chr(28) || chr(29) || chr(30) || chr(31) || chr(32) || chr(133) || chr(160) || chr(5760) || chr(8192) || chr(8193) || chr(8194) || chr(8195) || chr(8196) || chr(8197) || chr(8198) || chr(8199) || chr(8200) || chr(8201) || chr(8202) || chr(8232) || chr(8233) || chr(8239) || chr(8287) || chr(12288), '')) > 0"


def upgrade():
    op.create_unique_constraint(
        "uq_material_typed_id", "material_file", ["tenant_id", "id", "media_type"]
    )
    op.add_column(
        "account_material",
        sa.Column("media_type", sa.String(16), nullable=False, server_default="VIDEO"),
    )
    op.add_column(
        "account_material", sa.Column("is_carousel_usable", sa.Boolean(), nullable=True)
    )
    op.alter_column("account_material", "video_id", nullable=True)
    op.create_foreign_key(
        "fk_account_material_type",
        "account_material",
        "material_file",
        ["tenant_id", "material_id", "media_type"],
        ["tenant_id", "id", "media_type"],
    )
    op.create_check_constraint(
        "ck_account_material_type",
        "account_material",
        "(media_type = 'VIDEO' AND video_id IS NOT NULL AND is_carousel_usable IS NULL) OR (media_type = 'IMAGE' AND video_id IS NULL AND cover_url IS NULL)",
    )
    op.drop_constraint("ck_account_material_verified", "account_material")

    op.create_check_constraint(
        "ck_account_material_verified",
        "account_material",
        f"status != 'available' OR (verified_at IS NOT NULL AND ((media_type = 'VIDEO' AND {VIDEO_ID_NONEMPTY}) OR (media_type = 'IMAGE' AND image_id IS NOT NULL AND length(btrim(image_id)) > 0 AND is_carousel_usable IS TRUE)))",
    )
    op.alter_column("account_material", "media_type", server_default=None)


def downgrade():
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS(SELECT 1 FROM account_material WHERE media_type = 'IMAGE')"
            )
        )
        .scalar()
    ):
        raise RuntimeError("Image mappings cannot be downgraded to video identities")
    op.drop_constraint("fk_account_material_type", "account_material")
    op.drop_constraint("ck_account_material_type", "account_material")
    op.drop_constraint("ck_account_material_verified", "account_material")

    op.create_check_constraint(
        "ck_account_material_verified",
        "account_material",
        f"status != 'available' OR ({VIDEO_ID_NONEMPTY} AND verified_at IS NOT NULL)",
    )
    op.alter_column("account_material", "video_id", nullable=False)
    op.drop_column("account_material", "is_carousel_usable")
    op.drop_column("account_material", "media_type")
    op.drop_constraint("uq_material_typed_id", "material_file")
