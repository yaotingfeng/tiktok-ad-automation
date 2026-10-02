"""Allow the three newly integrated copyright provider kinds."""

import sqlalchemy as sa
from alembic import op

revision = "provider_kinds_expansion"
down_revision = "ad_management_cancelled"
branch_labels = None
depends_on = None

_NEW_KINDS = "kind IN ('wangyan','jiashu','duiba','gangganhao','rongliang','other')"
_OLD_KINDS = "kind IN ('wangyan','jiashu','other')"


def upgrade() -> None:
    op.drop_constraint(
        "ck_provider_connection_kind", "provider_connection", type_="check"
    )
    op.create_check_constraint(
        "ck_provider_connection_kind", "provider_connection", _NEW_KINDS
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM provider_connection WHERE kind IN ('duiba','gangganhao','rongliang'))"
        )
    ).scalar():
        raise RuntimeError(
            "Expanded provider connection data exists; remove it before downgrade"
        )
    op.drop_constraint(
        "ck_provider_connection_kind", "provider_connection", type_="check"
    )
    op.create_check_constraint(
        "ck_provider_connection_kind", "provider_connection", _OLD_KINDS
    )
