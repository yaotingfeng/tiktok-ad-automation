"""Type manual originals and ingest rows, preserving existing selection indexes."""

from alembic import op
import sqlalchemy as sa

revision = "0046_image_materials"
down_revision = "0045_directory_type_index"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("material_file", "ingest_session_file"):
        op.add_column(
            table,
            sa.Column(
                "media_type", sa.String(16), nullable=False, server_default="VIDEO"
            ),
        )
    op.create_check_constraint(
        "ck_material_media_type", "material_file", "media_type IN ('VIDEO','IMAGE')"
    )
    op.create_check_constraint(
        "ck_material_image_duration",
        "material_file",
        "media_type != 'IMAGE' OR duration IS NULL",
    )
    op.create_check_constraint(
        "ck_ingest_file_media_type",
        "ingest_session_file",
        "media_type IN ('VIDEO','IMAGE')",
    )
    # client_index 的持久化、唯一约束、翻页索引已存在，不能重建排序。
    # 旧 VIDEO duration 仍可为空，等待原件探测。


def downgrade():
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS(SELECT 1 FROM material_file WHERE media_type='IMAGE')"
            )
        )
        .scalar()
    ):
        raise RuntimeError(
            "Image originals exist; downgrade would erase media identity"
        )
    op.drop_constraint("ck_material_image_duration", "material_file", type_="check")
    op.drop_constraint("ck_material_media_type", "material_file", type_="check")
    op.drop_constraint(
        "ck_ingest_file_media_type", "ingest_session_file", type_="check"
    )
    op.drop_column("ingest_session_file", "media_type")
    op.drop_column("material_file", "media_type")
