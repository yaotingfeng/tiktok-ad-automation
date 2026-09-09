"""Replace platform email identifiers while retaining every user UUID and relation."""

import re

import sqlalchemy as sa
from alembic import op

revision = "0015_username_auth"
down_revision = "0014_recovery_candidates"
branch_labels = None
depends_on = None


def username_mapping(rows):
    """Pure deterministic mapping, also used for the private preflight export."""
    used = set()
    ordered = sorted(
        rows,
        key=lambda row: (
            row.email != "admin@example.com",
            row.email.lower() != "admin@example.com",
            str(row.id),
        ),
    )
    for row in ordered:
        base = re.sub(r"[^a-z0-9_.-]", "-", row.email.split("@", 1)[0].lower()).strip(
            ".-_"
        )[:64]
        if len(base) < 3:
            base = "user-" + row.id.hex
        candidate = base
        counter = 0
        while candidate in used:
            suffix = "-" + row.id.hex + (f"-{counter}" if counter else "")
            candidate = base[: 64 - len(suffix)] + suffix
            counter += 1
        used.add(candidate)
        yield row.id, candidate


def upgrade():
    op.add_column("user", sa.Column("username", sa.String(64), nullable=True))
    connection = op.get_bind()
    # User identities are small metadata, never business object/step tables.
    rows = connection.execute(sa.text('SELECT id, email FROM "user"')).all()
    for user_id, username in username_mapping(rows):
        connection.execute(
            sa.text('UPDATE "user" SET username=:username WHERE id=:id'),
            {"id": user_id, "username": username},
        )
    op.alter_column("user", "username", nullable=False)
    op.create_index("ix_user_username", "user", ["username"], unique=True)
    op.create_check_constraint(
        "ck_user_username_format", "user", "username ~ '^[a-z0-9_.-]{3,64}$'"
    )
    op.drop_index("ix_user_email", table_name="user")
    op.drop_column("user", "email")


def downgrade():
    raise RuntimeError(
        "Original account identifiers were removed. Restore the private pre-migration backup; automatic downgrade cannot preserve them."
    )
