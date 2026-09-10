"""Persist bounded ingest chunk receipts and local fingerprints

Revision ID: r2_ingest_chunks
Revises: r2_transient_ingest
Create Date: 2026-09-10 08:49:05.319664

"""

import sqlalchemy as sa
import sqlmodel.sql.sqltypes
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "r2_ingest_chunks"
down_revision = "r2_transient_ingest"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ingest_chunk",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "bc_id", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False
        ),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column(
            "request_digest",
            sqlmodel.sql.sqltypes.AutoString(length=64),
            nullable=False,
        ),
        sa.Column(
            "client_indexes", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "jsonb_array_length(client_indexes) BETWEEN 1 AND 200",
            name="ck_ingest_chunk_bound",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "bc_id", "session_id"],
            ["ingest_session.tenant_id", "ingest_session.bc_id", "ingest_session.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "session_id", "request_id", name="uq_ingest_chunk_request"
        ),
    )
    op.add_column(
        "ingest_session_file",
        sa.Column("last_modified_ms", sa.BigInteger(), nullable=True),
    )

    op.create_check_constraint(
        "ck_ingest_file_modified",
        "ingest_session_file",
        "last_modified_ms IS NULL OR last_modified_ms >= 0",
    )
    op.create_index(
        "ix_ingest_session_history",
        "ingest_session",
        ["tenant_id", "bc_id", "created_at", "id"],
    )
    op.create_index(
        "ix_ingest_file_status_seek",
        "ingest_session_file",
        ["tenant_id", "session_id", "status", "client_index", "material_id"],
    )
    op.execute("""
        CREATE FUNCTION guard_ingest_local_fingerprint() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            IF NEW.last_modified_ms IS DISTINCT FROM OLD.last_modified_ms THEN
                RAISE EXCEPTION 'ingest local fingerprint is immutable' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER ingest_local_fingerprint_immutable BEFORE UPDATE ON ingest_session_file
        FOR EACH ROW EXECUTE FUNCTION guard_ingest_local_fingerprint()
    """)

    op.create_index(
        "ix_ingest_transport_recovery",
        "temporary_material_object",
        ["status", "next_attempt_at", "id"],
        postgresql_where=sa.text(
            "error_code IN ('multipart_creating','multipart_create_unknown','multipart_collecting','multipart_completing','multipart_complete_unknown')"
        ),
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_ingest_transport_recovery")
    op.execute(
        "DROP TRIGGER IF EXISTS ingest_local_fingerprint_immutable ON ingest_session_file"
    )
    op.execute("DROP FUNCTION IF EXISTS guard_ingest_local_fingerprint()")
    op.execute("DROP INDEX IF EXISTS ix_ingest_file_status_seek")
    op.execute("DROP INDEX IF EXISTS ix_ingest_session_history")
    op.execute(
        "ALTER TABLE ingest_session_file DROP CONSTRAINT IF EXISTS ck_ingest_file_modified"
    )
    op.drop_column("ingest_session_file", "last_modified_ms")
    op.drop_table("ingest_chunk")
