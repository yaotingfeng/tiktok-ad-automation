"""Image authorization generation and explicit image reissue pairing.

Revision ID: 0048_image_reissue_routes
Revises: 0047_image_account_assets
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0048_image_reissue_routes"
down_revision = "0047_image_account_assets"
branch_labels = None
depends_on = None

OLD_GUARD = """
        CREATE OR REPLACE FUNCTION material_reissue_pair_guard() RETURNS trigger LANGUAGE plpgsql AS $$
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
"""
NEW_GUARD = OLD_GUARD.replace(
    "NEW.kind = 'VIDEO'", "NEW.kind IN ('VIDEO','IMAGE')"
).replace("a.kind='VIDEO'", "a.kind IN ('VIDEO','IMAGE')")
FROZEN_ROUTE_CONSTRAINT = "frozen_route IS NULL OR COALESCE((jsonb_typeof(frozen_route) = 'object' AND frozen_route ?& ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision'] AND frozen_route - ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision'] - 'binding_revision' = '{}'::jsonb AND (NOT frozen_route ? 'binding_revision' OR (jsonb_typeof(frozen_route->'binding_revision') = 'number' AND frozen_route->>'binding_revision' ~ '^[0-9]+$')) AND jsonb_typeof(frozen_route->'tenant_id') = 'string' AND jsonb_typeof(frozen_route->'bc_id') = 'string' AND jsonb_typeof(frozen_route->'connection_id') = 'string' AND jsonb_typeof(frozen_route->'channel') = 'string' AND jsonb_typeof(frozen_route->'authorization_revision') = 'number' AND jsonb_typeof(frozen_route->'adapter_contract_revision') = 'string' AND frozen_route->>'tenant_id' = tenant_id::text AND frozen_route->>'bc_id' = bc_id AND length(btrim(frozen_route->>'bc_id')) > 0 AND frozen_route->>'connection_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' AND frozen_route->>'channel' IN ('OFFICIAL_API','OFFICIAL_MCP') AND frozen_route->>'authorization_revision' ~ '^[0-9]+$' AND length(btrim(frozen_route->>'adapter_contract_revision')) > 0 AND frozen_route->>'connection_id' = connection_id::text), false)"


def _constraint(kinds):
    return f"(kind {kinds} AND old_distribution_id IS NOT NULL AND new_distribution_id IS NOT NULL AND old_distribution_id != new_distribution_id AND old_cover_job_id IS NULL AND new_cover_job_id IS NULL) OR (kind = 'COVER' AND old_cover_job_id IS NOT NULL AND new_cover_job_id IS NOT NULL AND old_cover_job_id != new_cover_job_id AND old_distribution_id IS NULL AND new_distribution_id IS NULL)"


def upgrade():
    op.add_column(
        "account_material",
        sa.Column("frozen_route", JSONB(none_as_null=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_account_material_frozen_route",
        "account_material",
        FROZEN_ROUTE_CONSTRAINT,
    )
    op.drop_constraint("ck_material_reissue_kind", "material_reissue_authorization")
    op.create_check_constraint(
        "ck_material_reissue_kind",
        "material_reissue_authorization",
        _constraint("IN ('VIDEO','IMAGE')"),
    )
    op.execute(NEW_GUARD)


def downgrade():
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS(SELECT 1 FROM material_reissue_authorization WHERE kind='IMAGE')"
            )
        )
        .scalar()
    ):
        raise RuntimeError("Cannot discard image reissue authorizations")
    op.execute(OLD_GUARD)
    op.drop_constraint("ck_material_reissue_kind", "material_reissue_authorization")
    op.create_check_constraint(
        "ck_material_reissue_kind",
        "material_reissue_authorization",
        _constraint("= 'VIDEO'"),
    )
    op.drop_constraint("ck_account_material_frozen_route", "account_material")
    op.drop_column("account_material", "frozen_route")
