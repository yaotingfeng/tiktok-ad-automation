"""Persist transient originals and ingest sessions

Revision ID: r2_transient_ingest
Revises: 0016_provider_session_refresh
Create Date: 2026-09-10 08:26:09.386872

"""

import sqlalchemy as sa
import sqlmodel.sql.sqltypes
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "r2_transient_ingest"
down_revision = "0016_provider_session_refresh"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "object_budget",
        sa.Column(
            "scope_key", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False
        ),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("reserved_bytes", sa.BigInteger(), nullable=False),
        sa.Column("stored_bytes", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "(scope_key = 'global' AND tenant_id IS NULL) OR (tenant_id IS NOT NULL AND scope_key = 'tenant:' || tenant_id::text)",
            name="ck_object_budget_scope",
        ),
        sa.CheckConstraint(
            "reserved_bytes >= 0 AND stored_bytes >= 0 AND stored_bytes <= reserved_bytes AND revision >= 0",
            name="ck_object_budget_numbers",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
        ),
        sa.PrimaryKeyConstraint("scope_key"),
    )
    op.create_table(
        "ingest_session",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "bc_id", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False
        ),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column(
            "request_digest",
            sqlmodel.sql.sqltypes.AutoString(length=64),
            nullable=False,
        ),
        sa.Column("expected_files", sa.Integer(), nullable=False),
        sa.Column("expected_bytes", sa.BigInteger(), nullable=False),
        sa.Column("registration_cursor", sa.Integer(), nullable=False),
        sa.Column(
            "status", sqlmodel.sql.sqltypes.AutoString(length=32), nullable=False
        ),
        sa.Column("accepted_count", sa.Integer(), nullable=False),
        sa.Column("uploaded_count", sa.Integer(), nullable=False),
        sa.Column("ready_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("cleaned_count", sa.Integer(), nullable=False),
        sa.Column("accepted_bytes", sa.BigInteger(), nullable=False),
        sa.Column("uploaded_bytes", sa.BigInteger(), nullable=False),
        sa.Column("ready_bytes", sa.BigInteger(), nullable=False),
        sa.Column("cleaned_bytes", sa.BigInteger(), nullable=False),
        sa.Column("reserved_bytes", sa.BigInteger(), nullable=False),
        sa.Column("stored_bytes", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("dispatch_id", sa.Uuid(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "accepted_bytes >= 0 AND uploaded_bytes >= 0 AND ready_bytes >= 0 AND cleaned_bytes >= 0 AND accepted_bytes <= expected_bytes AND uploaded_bytes <= accepted_bytes AND ready_bytes <= accepted_bytes AND cleaned_bytes <= accepted_bytes",
            name="ck_ingest_session_bytes",
        ),
        sa.CheckConstraint(
            "accepted_count >= 0 AND uploaded_count >= 0 AND ready_count >= 0 AND failed_count >= 0 AND cleaned_count >= 0 AND accepted_count <= expected_files AND uploaded_count <= accepted_count AND ready_count <= accepted_count AND cleaned_count <= accepted_count AND failed_count <= accepted_count",
            name="ck_ingest_session_counts",
        ),
        sa.CheckConstraint(
            "expected_files > 0 AND expected_bytes > 0 AND registration_cursor >= 0 AND revision >= 0",
            name="ck_ingest_session_manifest",
        ),
        sa.CheckConstraint(
            "reserved_bytes >= 0 AND stored_bytes >= 0 AND stored_bytes <= reserved_bytes",
            name="ck_ingest_session_occupancy",
        ),
        sa.ForeignKeyConstraint(
            ["dispatch_id"],
            ["pending_dispatch.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "actor_id"],
            ["tenant_membership.tenant_id", "tenant_membership.user_id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "bc_id"],
            ["tenant_bc.tenant_id", "tenant_bc.bc_id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "bc_id", "id", name="uq_ingest_session_scope"),
        sa.UniqueConstraint(
            "tenant_id", "request_id", name="uq_ingest_session_request"
        ),
    )
    op.create_index(
        "ix_ingest_session_recovery",
        "ingest_session",
        ["status", "next_attempt_at", "id"],
        unique=False,
    )
    op.create_table(
        "temporary_material_object",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "bc_id", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False
        ),
        sa.Column("material_id", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column(
            "object_key", sqlmodel.sql.sqltypes.AutoString(length=512), nullable=False
        ),
        sa.Column("expected_bytes", sa.BigInteger(), nullable=False),
        sa.Column("actual_bytes", sa.BigInteger(), nullable=True),
        sa.Column("reserved_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "status", sqlmodel.sql.sqltypes.AutoString(length=32), nullable=False
        ),
        sa.Column("s3_upload_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("part_size", sa.BigInteger(), nullable=False),
        sa.Column("parts", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("sha256", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True),
        sa.Column(
            "video_md5", sqlmodel.sql.sqltypes.AutoString(length=32), nullable=True
        ),
        sa.Column("digest_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "digest_source", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("claim_token", sa.Uuid(), nullable=True),
        sa.Column("claimed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reservation_released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "error_code", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=True
        ),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status != 'verified' OR (sha256 IS NOT NULL AND video_md5 IS NOT NULL AND digest_verified_at IS NOT NULL AND actual_bytes IS NOT NULL AND actual_bytes = expected_bytes)",
            name="ck_temporary_object_verified",
        ),
        sa.CheckConstraint(
            "status IN ('waiting_capacity','reserved','receiving','stored','validating','verified','cleanup_pending','deleting','deleted','delete_unknown','missing')",
            name="ck_temporary_object_status",
        ),
        sa.CheckConstraint(
            "(claim_token IS NULL) = (claimed_until IS NULL)",
            name="ck_temporary_object_claim",
        ),
        sa.CheckConstraint(
            "generation > 0 AND expected_bytes > 0 AND (actual_bytes IS NULL OR actual_bytes > 0) AND reserved_bytes >= 0 AND revision >= 0 AND part_size > 0",
            name="ck_temporary_object_numbers",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "bc_id", "material_id"],
            ["material_file.tenant_id", "material_file.bc_id", "material_file.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("object_key", name="uq_temporary_object_key"),
        sa.UniqueConstraint(
            "tenant_id",
            "bc_id",
            "material_id",
            "generation",
            name="uq_temporary_object_generation",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_temporary_object_scope"),
    )
    op.create_index(
        "ix_temporary_object_recovery",
        "temporary_material_object",
        ["status", "next_attempt_at", "id"],
        unique=False,
    )
    op.create_table(
        "object_cleanup",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "bc_id", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False
        ),
        sa.Column("material_id", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column(
            "reason", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False
        ),
        sa.Column(
            "eligibility_evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "status", sqlmodel.sql.sqltypes.AutoString(length=32), nullable=False
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("claim_token", sa.Uuid(), nullable=True),
        sa.Column("claimed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dispatch_id", sa.Uuid(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("delete_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("abort_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delete_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("head_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "error_code", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=True
        ),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending','deleting','delete_unknown','deleted','blocked') AND revision >= 0 AND attempt_count >= 0",
            name="ck_object_cleanup_status",
        ),
        sa.CheckConstraint(
            "(claim_token IS NULL) = (claimed_until IS NULL)",
            name="ck_object_cleanup_claim",
        ),
        sa.ForeignKeyConstraint(
            ["dispatch_id"],
            ["pending_dispatch.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "bc_id", "material_id", "generation"],
            [
                "temporary_material_object.tenant_id",
                "temporary_material_object.bc_id",
                "temporary_material_object.material_id",
                "temporary_material_object.generation",
            ],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "bc_id",
            "material_id",
            "generation",
            name="uq_object_cleanup_generation",
        ),
    )
    op.create_index(
        "ix_object_cleanup_recovery",
        "object_cleanup",
        ["status", "next_attempt_at", "id"],
        unique=False,
    )
    op.create_table(
        "original_use",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "bc_id", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False
        ),
        sa.Column("material_id", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column(
            "purpose", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False
        ),
        sa.Column("operation_id", sa.Uuid(), nullable=True),
        sa.Column("dispatch_id", sa.Uuid(), nullable=True),
        sa.Column("nonce", sa.Uuid(), nullable=False),
        sa.Column(
            "status", sqlmodel.sql.sqltypes.AutoString(length=16), nullable=False
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("permission_issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "revision >= 0 AND status IN ('active','released','expired')",
            name="ck_original_use_status",
        ),
        sa.ForeignKeyConstraint(
            ["dispatch_id"],
            ["pending_dispatch.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "actor_id"],
            ["tenant_membership.tenant_id", "tenant_membership.user_id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "bc_id", "material_id", "generation"],
            [
                "temporary_material_object.tenant_id",
                "temporary_material_object.bc_id",
                "temporary_material_object.material_id",
                "temporary_material_object.generation",
            ],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("nonce"),
    )
    op.create_index(
        "ix_original_use_expiry",
        "original_use",
        ["status", "expires_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_original_use_object",
        "original_use",
        ["tenant_id", "bc_id", "material_id", "generation", "status", "expires_at"],
        unique=False,
    )
    op.create_table(
        "ingest_session_file",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "bc_id", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False
        ),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("client_index", sa.Integer(), nullable=False),
        sa.Column("material_id", sa.Uuid(), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column(
            "manifest_digest",
            sqlmodel.sql.sqltypes.AutoString(length=64),
            nullable=False,
        ),
        sa.Column(
            "status", sqlmodel.sql.sqltypes.AutoString(length=32), nullable=False
        ),
        sa.Column("current_generation", sa.Integer(), nullable=False),
        sa.Column(
            "source_advertiser_id",
            sqlmodel.sql.sqltypes.AutoString(length=128),
            nullable=True,
        ),
        sa.Column("connection_id", sa.Uuid(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "error_code", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=True
        ),
        sa.Column("dispatch_id", sa.Uuid(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('registered','waiting_capacity','receiving','stored','validating','uploading','verifying','available','failed','blocked','result_unknown','cancelled')",
            name="ck_ingest_file_status",
        ),
        sa.CheckConstraint(
            "(source_advertiser_id IS NULL) = (connection_id IS NULL)",
            name="ck_ingest_file_source",
        ),
        sa.CheckConstraint(
            "client_index >= 0 AND byte_size > 0 AND current_generation > 0 AND revision >= 0",
            name="ck_ingest_file_numbers",
        ),
        sa.ForeignKeyConstraint(
            ["dispatch_id"],
            ["pending_dispatch.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "bc_id", "material_id"],
            ["material_file.tenant_id", "material_file.bc_id", "material_file.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "bc_id", "session_id"],
            ["ingest_session.tenant_id", "ingest_session.bc_id", "ingest_session.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "bc_id", "source_advertiser_id", "connection_id"],
            [
                "bc_account_access.tenant_id",
                "bc_account_access.bc_id",
                "bc_account_access.advertiser_id",
                "bc_account_access.connection_id",
            ],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "bc_id",
            "session_id",
            "material_id",
            name="uq_ingest_file_material",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_ingest_file_scope"),
        sa.UniqueConstraint(
            "tenant_id", "session_id", "client_index", name="uq_ingest_client_index"
        ),
    )
    op.create_index(
        "ix_ingest_file_recovery",
        "ingest_session_file",
        ["status", "next_attempt_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_ingest_file_seek",
        "ingest_session_file",
        ["tenant_id", "session_id", "client_index", "material_id"],
        unique=False,
    )
    op.create_table(
        "source_account_load",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "bc_id", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False
        ),
        sa.Column(
            "advertiser_id",
            sqlmodel.sql.sqltypes.AutoString(length=128),
            nullable=False,
        ),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("in_flight", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("last_assigned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "in_flight >= 0 AND revision >= 0", name="ck_source_load_numbers"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "bc_id", "advertiser_id", "connection_id"],
            [
                "bc_account_access.tenant_id",
                "bc_account_access.bc_id",
                "bc_account_access.advertiser_id",
                "bc_account_access.connection_id",
            ],
        ),
        sa.PrimaryKeyConstraint("tenant_id", "bc_id", "advertiser_id", "connection_id"),
    )
    op.create_table(
        "ingest_milestone",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "bc_id", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False
        ),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("material_id", sa.Uuid(), nullable=False),
        sa.Column(
            "milestone", sqlmodel.sql.sqltypes.AutoString(length=16), nullable=False
        ),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "milestone IN ('accepted','uploaded','ready','cleaned') AND byte_size > 0",
            name="ck_ingest_milestone",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "bc_id", "session_id", "material_id"],
            [
                "ingest_session_file.tenant_id",
                "ingest_session_file.bc_id",
                "ingest_session_file.session_id",
                "ingest_session_file.material_id",
            ],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id", "material_id", "milestone", name="uq_ingest_milestone"
        ),
    )
    op.create_table(
        "ingest_transition",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("file_id", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "from_status", sqlmodel.sql.sqltypes.AutoString(length=32), nullable=False
        ),
        sa.Column(
            "to_status", sqlmodel.sql.sqltypes.AutoString(length=32), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "revision > 0 AND generation > 0", name="ck_ingest_transition_numbers"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "file_id"],
            ["ingest_session_file.tenant_id", "ingest_session_file.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "file_id", "revision", name="uq_ingest_transition_revision"
        ),
    )
    op.add_column(
        "material_file",
        sa.Column("current_object_generation", sa.Integer(), nullable=True),
    )
    op.add_column(
        "material_file",
        sa.Column("digest_verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "material_file",
        sa.Column(
            "digest_source", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True
        ),
    )

    op.add_column(
        "temporary_material_object", sa.Column("storage_provider", sa.String(32))
    )
    op.add_column(
        "temporary_material_object", sa.Column("storage_endpoint", sa.String(512))
    )
    op.add_column(
        "temporary_material_object", sa.Column("storage_bucket", sa.String(255))
    )
    # Keep historical keys and mappings unchanged. Stored legacy checksums are
    # not promoted to verified evidence merely by running a migration.
    op.execute("""
        INSERT INTO temporary_material_object (
            id, tenant_id, bc_id, material_id, generation, object_key,
            expected_bytes, actual_bytes, reserved_bytes, status, s3_upload_id,
            part_size, parts, sha256, video_md5, revision, reserved_at,
            received_at, next_attempt_at, created_at
        )
        SELECT m.id, m.tenant_id, m.bc_id, m.id, 1, m.object_key,
            m.byte_size, CASE WHEN m.storage_state = 'stored' THEN m.byte_size END,
            CASE WHEN m.storage_state = 'unavailable' THEN 0 ELSE m.byte_size END,
            CASE WHEN m.storage_state = 'unavailable' THEN 'missing' ELSE m.storage_state END,
            u.s3_upload_id, COALESCE(u.part_size, 16777216), COALESCE(u.parts, '[]'::jsonb),
            m.sha256, m.video_md5, 0,
            CASE WHEN m.storage_state != 'unavailable' THEN now() END,
            CASE WHEN m.storage_state = 'stored' THEN m.created_at END,
            now(), m.created_at
        FROM material_file m LEFT JOIN object_upload u
            ON u.tenant_id = m.tenant_id AND u.bc_id = m.bc_id AND u.material_id = m.id
    """)
    op.execute("UPDATE material_file SET current_object_generation = 1")
    op.execute("""
        INSERT INTO object_budget (scope_key, tenant_id, reserved_bytes, stored_bytes, revision)
        SELECT 'tenant:' || tenant_id::text, tenant_id, SUM(reserved_bytes),
               SUM(COALESCE(actual_bytes, 0)), 0
        FROM temporary_material_object GROUP BY tenant_id
    """)
    op.execute("""
        INSERT INTO object_budget (scope_key, tenant_id, reserved_bytes, stored_bytes, revision)
        SELECT 'global', NULL, COALESCE(SUM(reserved_bytes), 0),
               COALESCE(SUM(actual_bytes), 0), 0 FROM temporary_material_object
    """)
    op.execute("""
        CREATE FUNCTION guard_temporary_object_identity() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF (NEW.id, NEW.tenant_id, NEW.bc_id, NEW.material_id,
                NEW.generation, NEW.object_key, NEW.expected_bytes)
                IS DISTINCT FROM
               (OLD.id, OLD.tenant_id, OLD.bc_id, OLD.material_id,
                OLD.generation, OLD.object_key, OLD.expected_bytes) THEN
                RAISE EXCEPTION 'temporary object identity is immutable'
                    USING ERRCODE = '23514';
            END IF;
            IF OLD.storage_provider IS NOT NULL AND
               (NEW.storage_provider, NEW.storage_endpoint, NEW.storage_bucket)
               IS DISTINCT FROM
               (OLD.storage_provider, OLD.storage_endpoint, OLD.storage_bucket) THEN
                RAISE EXCEPTION 'temporary object storage binding is immutable'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER temporary_object_identity_immutable
        BEFORE UPDATE ON temporary_material_object FOR EACH ROW
        EXECUTE FUNCTION guard_temporary_object_identity()
    """)

    op.execute("""
        CREATE FUNCTION guard_ingest_manifest_identity() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF (NEW.id, NEW.tenant_id, NEW.bc_id, NEW.session_id, NEW.client_index,
                NEW.material_id, NEW.byte_size, NEW.manifest_digest)
                IS DISTINCT FROM
               (OLD.id, OLD.tenant_id, OLD.bc_id, OLD.session_id, OLD.client_index,
                OLD.material_id, OLD.byte_size, OLD.manifest_digest) THEN
                RAISE EXCEPTION 'ingest manifest identity is immutable'
                    USING ERRCODE = '23514';
            END IF;
            IF NEW.current_generation < OLD.current_generation THEN
                RAISE EXCEPTION 'object generation cannot move backwards'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER ingest_manifest_identity_immutable
        BEFORE UPDATE ON ingest_session_file FOR EACH ROW
        EXECUTE FUNCTION guard_ingest_manifest_identity()
    """)


def downgrade():
    op.execute(
        "DROP TRIGGER IF EXISTS ingest_manifest_identity_immutable ON ingest_session_file"
    )
    op.execute("DROP FUNCTION IF EXISTS guard_ingest_manifest_identity()")
    # Rollback can remove metadata, never recover already deleted object bytes.
    # Before reverting readers, conservatively mark nonusable current objects.
    # The object ledger is independently deletable; a missing current row must
    # not regain the legacy stored fallback when the generation marker is dropped.
    op.execute("""
        UPDATE material_file m SET storage_state = 'unavailable'
        WHERE m.current_object_generation IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM temporary_material_object o
            WHERE o.tenant_id = m.tenant_id AND o.bc_id = m.bc_id
              AND o.material_id = m.id AND o.generation = m.current_object_generation
        )
    """)
    op.execute("""
        UPDATE material_file m SET object_key = o.object_key, storage_state = CASE
            WHEN o.status IN ('stored','validating','verified') THEN 'stored'
            WHEN o.status IN ('waiting_capacity','reserved','receiving') THEN 'receiving'
            ELSE 'unavailable' END
        FROM temporary_material_object o
        WHERE o.tenant_id = m.tenant_id AND o.bc_id = m.bc_id
          AND o.material_id = m.id AND o.generation = m.current_object_generation
    """)
    op.execute(
        "DROP TRIGGER temporary_object_identity_immutable ON temporary_material_object"
    )
    op.execute("DROP FUNCTION guard_temporary_object_identity()")
    op.drop_column("material_file", "digest_source")
    op.drop_column("material_file", "digest_verified_at")
    op.drop_column("material_file", "current_object_generation")
    op.drop_table("ingest_transition")
    op.drop_table("ingest_milestone")
    op.drop_table("source_account_load")
    op.drop_index("ix_ingest_file_seek", table_name="ingest_session_file")
    op.drop_index("ix_ingest_file_recovery", table_name="ingest_session_file")
    op.drop_table("ingest_session_file")
    op.drop_index("ix_original_use_object", table_name="original_use")
    op.drop_index("ix_original_use_expiry", table_name="original_use")
    op.drop_table("original_use")
    op.drop_index("ix_object_cleanup_recovery", table_name="object_cleanup")
    op.drop_table("object_cleanup")
    op.drop_index(
        "ix_temporary_object_recovery", table_name="temporary_material_object"
    )
    op.drop_table("temporary_material_object")
    op.drop_index("ix_ingest_session_recovery", table_name="ingest_session")
    op.drop_table("ingest_session")
    op.drop_table("object_budget")
