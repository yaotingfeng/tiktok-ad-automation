"""Reject whitespace-only available video identities, retaining invalid history.

Revision ID: 0004b_material_checks
Revises: 0004_materials
"""

from alembic import op

revision = "0004b_material_checks"
down_revision = "0004_materials"
branch_labels = None
depends_on = None

# Freeze the whitespace set here; migrations never import mutable model code.
_SPACES = (
    *range(9, 14),
    *range(28, 33),
    133,
    160,
    5760,
    *range(8192, 8203),
    8232,
    8233,
    8239,
    8287,
    12288,
)
_NONEMPTY = (
    "length(translate(video_id, "
    + " || ".join(f"chr({value})" for value in _SPACES)
    + ", '')) > 0"
)


def upgrade() -> None:
    op.execute(
        f"UPDATE account_material SET status='unavailable' WHERE status='available' AND NOT ({_NONEMPTY})"
    )
    op.drop_constraint(
        "ck_account_material_verified", "account_material", type_="check"
    )
    op.create_check_constraint(
        "ck_account_material_verified",
        "account_material",
        f"status != 'available' OR ({_NONEMPTY} AND verified_at IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_account_material_verified", "account_material", type_="check"
    )
    op.create_check_constraint(
        "ck_account_material_verified",
        "account_material",
        "status != 'available' OR (video_id <> '' AND verified_at IS NOT NULL)",
    )
