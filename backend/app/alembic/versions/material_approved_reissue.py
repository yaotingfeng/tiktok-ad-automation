"""明确接受重复素材的一次补发；保留旧 UNKNOWN 与所有远端证据。

Revision ID: material_approved_reissue
Revises: material_route_scope
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "material_approved_reissue"
down_revision = "material_route_scope"
branch_labels = None
depends_on = None

TABLES = ("material_asset_operation", "material_distribution", "material_cover_job")
NAMES = ("operation", "distribution", "cover")
INDEXES = (
    (
        "material_asset_operation",
        "uq_material_unverified_operation",
        "status IN ('pending','sending','result_unknown','verifying','confirmed_absent')",
    ),
    (
        "material_distribution",
        "uq_material_pending_distribution",
        "status IN ('queued','preparing','verifying','result_unknown')",
    ),
)


def upgrade() -> None:
    for table, name in zip(TABLES, NAMES, strict=True):
        op.add_column(table, sa.Column("superseded_by_id", sa.UUID(), nullable=True))
        scope = (
            ["tenant_id"]
            if name == "cover"
            else ["tenant_id", "bc_id", "material_id", "advertiser_id"]
        )
        op.create_foreign_key(
            f"fk_material_{name}_superseded",
            table,
            table,
            [*scope, "superseded_by_id"],
            [*scope, "id"],
            deferrable=True,
            initially="DEFERRED",
        )
        op.create_check_constraint(
            f"ck_material_{name}_no_self",
            table,
            "superseded_by_id IS NULL OR superseded_by_id != id",
        )
    op.create_unique_constraint(
        "uq_material_distribution_tenant", "material_distribution", ["tenant_id", "id"]
    )
    for table, index, predicate in INDEXES:
        op.drop_index(index, table_name=table)
        op.create_index(
            index,
            table,
            ["tenant_id", "bc_id", "material_id", "advertiser_id"],
            unique=True,
            postgresql_where=sa.text(predicate + " AND superseded_by_id IS NULL"),
        )
    op.drop_constraint("uq_material_cover_video", "material_cover_job", type_="unique")
    op.create_index(
        "uq_material_cover_video",
        "material_cover_job",
        ["tenant_id", "asset_id", "connection_id", "video_id"],
        unique=True,
        postgresql_where=sa.text("superseded_by_id IS NULL"),
    )
    op.create_table(
        "material_reissue_authorization",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("bc_id", sa.String(128), nullable=False),
        sa.Column("submission_id", sa.UUID(), nullable=False),
        sa.Column("request_id", sa.UUID(), nullable=False),
        sa.Column("actor_id", sa.UUID(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("old_distribution_id", sa.UUID(), nullable=True),
        sa.Column("new_distribution_id", sa.UUID(), nullable=True),
        sa.Column("old_cover_job_id", sa.UUID(), nullable=True),
        sa.Column("new_cover_job_id", sa.UUID(), nullable=True),
        sa.Column("scope_digest", sa.String(64), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=False),
        sa.Column("accepted_duplicate_materials", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "request_id", name="uq_material_reissue_request"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "bc_id"], ["tenant_bc.tenant_id", "tenant_bc.bc_id"]
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "submission_id"],
            ["build_submission.tenant_id", "build_submission.id"],
        ),
        sa.CheckConstraint(
            "(kind = 'VIDEO' AND old_distribution_id IS NOT NULL AND new_distribution_id IS NOT NULL "
            "AND old_distribution_id != new_distribution_id AND old_cover_job_id IS NULL AND new_cover_job_id IS NULL) OR "
            "(kind = 'COVER' AND old_cover_job_id IS NOT NULL AND new_cover_job_id IS NOT NULL "
            "AND old_cover_job_id != new_cover_job_id AND old_distribution_id IS NULL AND new_distribution_id IS NULL)",
            name="ck_material_reissue_kind",
        ),
        sa.CheckConstraint(
            "accepted_duplicate_materials", name="ck_material_reissue_accepted"
        ),
        sa.CheckConstraint(
            "scope_digest ~ '^[0-9a-f]{64}$'", name="ck_material_reissue_digest"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(details) = 'object'", name="ck_material_reissue_details"
        ),
    )
    for column, table, name in (
        ("old_distribution_id", "material_distribution", "old_distribution"),
        ("new_distribution_id", "material_distribution", "new_distribution"),
        ("old_cover_job_id", "material_cover_job", "old_cover"),
        ("new_cover_job_id", "material_cover_job", "new_cover"),
    ):
        op.create_unique_constraint(
            f"uq_material_reissue_{name}", "material_reissue_authorization", [column]
        )
        op.create_foreign_key(
            f"fk_material_reissue_{name}",
            "material_reissue_authorization",
            table,
            ["tenant_id", column],
            ["tenant_id", "id"],
        )
    op.execute("""
        CREATE FUNCTION material_reissue_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'immutable material reissue authorization' USING ERRCODE='23514';
        END $$;
        CREATE TRIGGER material_reissue_immutable BEFORE UPDATE OR DELETE
        ON material_reissue_authorization FOR EACH ROW EXECUTE FUNCTION material_reissue_immutable();

        CREATE FUNCTION material_replacement_pointer_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'UPDATE' AND OLD.superseded_by_id IS NOT NULL
               AND NEW.superseded_by_id IS DISTINCT FROM OLD.superseded_by_id THEN
                RAISE EXCEPTION 'immutable material replacement pointer' USING ERRCODE='23514';
            END IF;
            IF NEW.superseded_by_id IS NOT NULL AND
               (TG_OP = 'INSERT' OR OLD.superseded_by_id IS NULL) THEN
                IF (TG_TABLE_NAME = 'material_cover_job' AND NEW.status != 'UNKNOWN') OR
                   (TG_TABLE_NAME != 'material_cover_job' AND NEW.status != 'result_unknown') OR
                   (TG_OP = 'UPDATE' AND OLD.status IS DISTINCT FROM NEW.status) THEN
                    RAISE EXCEPTION 'material replacement requires UNKNOWN evidence' USING ERRCODE='23514';
                END IF;
            END IF;
            RETURN NEW;
        END $$;
    """)
    # 授权在 helper flush 后插入，同一事务结束才核验配对；不允许裸改指针绕过授权。
    op.execute("""
        CREATE FUNCTION material_reissue_pair_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE matched boolean;
        BEGIN
            IF TG_TABLE_NAME = 'material_reissue_authorization' THEN
                SELECT EXISTS (SELECT 1 FROM build_submission s
                    WHERE s.tenant_id=NEW.tenant_id AND s.id=NEW.submission_id AND s.bc_id=NEW.bc_id)
                INTO matched;
                IF NOT matched THEN
                    RAISE EXCEPTION 'material reissue scope mismatch' USING ERRCODE='23514';
                END IF;
                IF NEW.kind = 'VIDEO' THEN
                    SELECT EXISTS (SELECT 1 FROM material_distribution prior
                        JOIN material_distribution replacement ON replacement.id=NEW.new_distribution_id
                          AND replacement.tenant_id=prior.tenant_id AND replacement.bc_id=prior.bc_id
                          AND replacement.material_id=prior.material_id AND replacement.advertiser_id=prior.advertiser_id
                        WHERE prior.tenant_id=NEW.tenant_id AND prior.bc_id=NEW.bc_id
                          AND prior.id=NEW.old_distribution_id AND prior.superseded_by_id=replacement.id
                          AND (prior.operation_id IS NULL OR EXISTS (
                            SELECT 1 FROM material_asset_operation operation
                            WHERE operation.tenant_id=prior.tenant_id AND operation.id=prior.operation_id
                              AND operation.superseded_by_id=replacement.operation_id))) INTO matched;
                ELSE
                    SELECT EXISTS (SELECT 1 FROM material_cover_job prior
                        JOIN material_cover_job replacement ON replacement.id=NEW.new_cover_job_id
                          AND replacement.tenant_id=prior.tenant_id AND replacement.bc_id=prior.bc_id
                          AND replacement.material_id=prior.material_id AND replacement.asset_id=prior.asset_id
                          AND replacement.advertiser_id=prior.advertiser_id AND replacement.connection_id=prior.connection_id
                          AND replacement.video_id=prior.video_id
                        WHERE prior.tenant_id=NEW.tenant_id AND prior.bc_id=NEW.bc_id
                          AND prior.id=NEW.old_cover_job_id AND prior.superseded_by_id=replacement.id) INTO matched;
                END IF;
                IF NOT matched THEN
                    RAISE EXCEPTION 'material reissue scope or generation mismatch' USING ERRCODE='23514';
                END IF;
                RETURN NEW;
            END IF;
            IF NEW.superseded_by_id IS NULL THEN RETURN NEW; END IF;
            IF TG_TABLE_NAME = 'material_distribution' THEN
                SELECT EXISTS (SELECT 1 FROM material_reissue_authorization a
                    WHERE a.tenant_id=NEW.tenant_id AND a.kind='VIDEO'
                      AND a.old_distribution_id=NEW.id AND a.new_distribution_id=NEW.superseded_by_id) INTO matched;
            ELSIF TG_TABLE_NAME = 'material_cover_job' THEN
                SELECT EXISTS (SELECT 1 FROM material_reissue_authorization a
                    WHERE a.tenant_id=NEW.tenant_id AND a.kind='COVER'
                      AND a.old_cover_job_id=NEW.id AND a.new_cover_job_id=NEW.superseded_by_id) INTO matched;
            ELSE
                SELECT EXISTS (SELECT 1 FROM material_reissue_authorization a
                    JOIN material_distribution prior ON prior.id=a.old_distribution_id AND prior.tenant_id=a.tenant_id
                    JOIN material_distribution replacement ON replacement.id=a.new_distribution_id AND replacement.tenant_id=a.tenant_id
                    WHERE a.tenant_id=NEW.tenant_id AND a.kind='VIDEO'
                      AND prior.operation_id=NEW.id AND replacement.operation_id=NEW.superseded_by_id) INTO matched;
            END IF;
            IF NOT matched THEN
                RAISE EXCEPTION 'material reissue authorization required' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$;
    """)
    for table in TABLES:
        op.execute(
            f"CREATE TRIGGER material_replacement_pointer_guard BEFORE INSERT OR UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION material_replacement_pointer_guard()"
        )
        op.execute(
            f"CREATE CONSTRAINT TRIGGER material_reissue_pair_guard AFTER INSERT OR UPDATE OF superseded_by_id ON {table} "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION material_reissue_pair_guard()"
        )
    op.execute(
        "CREATE CONSTRAINT TRIGGER material_reissue_pair_guard AFTER INSERT ON material_reissue_authorization "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION material_reissue_pair_guard()"
    )


def downgrade() -> None:
    # 一旦有人接受重复素材，降级会丢失授权及替代证据；明确拒绝而不是清理历史。
    connection = op.get_bind()
    op.execute(
        "LOCK TABLE material_reissue_authorization, material_asset_operation, material_distribution, material_cover_job IN ACCESS EXCLUSIVE MODE"
    )
    if connection.exec_driver_sql(
        "SELECT EXISTS (SELECT 1 FROM material_reissue_authorization) OR "
        + " OR ".join(
            f"EXISTS (SELECT 1 FROM {table} WHERE superseded_by_id IS NOT NULL)"
            for table in TABLES
        )
    ).scalar_one():
        raise RuntimeError("Cannot downgrade used material reissue evidence")
    for table in TABLES:
        op.execute(f"DROP TRIGGER material_reissue_pair_guard ON {table}")
        op.execute(f"DROP TRIGGER material_replacement_pointer_guard ON {table}")
    op.drop_table("material_reissue_authorization")
    op.execute("DROP FUNCTION material_reissue_pair_guard()")
    op.execute("DROP FUNCTION material_replacement_pointer_guard()")
    op.execute("DROP FUNCTION material_reissue_immutable()")
    op.drop_index("uq_material_cover_video", table_name="material_cover_job")
    op.create_unique_constraint(
        "uq_material_cover_video",
        "material_cover_job",
        ["tenant_id", "asset_id", "connection_id", "video_id"],
    )
    for table, index, predicate in INDEXES:
        op.drop_index(index, table_name=table)
        op.create_index(
            index,
            table,
            ["tenant_id", "bc_id", "material_id", "advertiser_id"],
            unique=True,
            postgresql_where=sa.text(predicate),
        )
    op.drop_constraint(
        "uq_material_distribution_tenant", "material_distribution", type_="unique"
    )
    for table, name in zip(TABLES, NAMES, strict=True):
        op.drop_constraint(f"fk_material_{name}_superseded", table, type_="foreignkey")
        op.drop_constraint(f"ck_material_{name}_no_self", table, type_="check")
        op.drop_column(table, "superseded_by_id")
