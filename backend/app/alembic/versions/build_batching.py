"""批量共享冻结身份/物理回执与已核实补建台账，不改写历史尝试。"""

from alembic import op

revision = "build_batching"
down_revision = "provider_display_drama_id"
branch_labels = None
depends_on = None

# 固定 SQL 是本次审查的 schema 快照；历史迁移不导入会继续变化的业务模型。
SCHEMA = r"""CREATE TABLE material_share_batch (
	id UUID NOT NULL,
	tenant_id UUID NOT NULL,
	bc_id VARCHAR(128) NOT NULL,
	actor_id UUID NOT NULL,
	source_advertiser_id VARCHAR(128) NOT NULL,
	target_route JSONB NOT NULL,
	source_route JSONB NOT NULL,
	request_digest VARCHAR(64) NOT NULL,
	wire_digest VARCHAR(64),
	claim_id UUID NOT NULL,
	status VARCHAR NOT NULL,
	material_ids JSONB NOT NULL,
	advertiser_ids JSONB NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_material_share_batch_scope UNIQUE (tenant_id, bc_id, id),
	FOREIGN KEY(tenant_id, bc_id) REFERENCES tenant_bc (tenant_id, bc_id),
	CONSTRAINT ck_material_share_batch_target_route CHECK (target_route IS NULL OR COALESCE((jsonb_typeof(target_route) = 'object' AND target_route ?& ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision'] AND target_route - ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision'] - 'binding_revision' = '{}'::jsonb AND (NOT target_route ? 'binding_revision' OR (jsonb_typeof(target_route->'binding_revision') = 'number' AND target_route->>'binding_revision' ~ '^[0-9]+$')) AND jsonb_typeof(target_route->'tenant_id') = 'string' AND jsonb_typeof(target_route->'bc_id') = 'string' AND jsonb_typeof(target_route->'connection_id') = 'string' AND jsonb_typeof(target_route->'channel') = 'string' AND jsonb_typeof(target_route->'authorization_revision') = 'number' AND jsonb_typeof(target_route->'adapter_contract_revision') = 'string' AND target_route->>'tenant_id' = tenant_id::text AND target_route->>'bc_id' = bc_id AND length(btrim(target_route->>'bc_id')) > 0 AND target_route->>'connection_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' AND target_route->>'channel' IN ('OFFICIAL_API','OFFICIAL_MCP') AND target_route->>'authorization_revision' ~ '^[0-9]+$' AND length(btrim(target_route->>'adapter_contract_revision')) > 0), false)),
	CONSTRAINT ck_material_share_batch_source_route CHECK (source_route IS NULL OR COALESCE((jsonb_typeof(source_route) = 'object' AND source_route ?& ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision'] AND source_route - ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision'] - 'binding_revision' = '{}'::jsonb AND (NOT source_route ? 'binding_revision' OR (jsonb_typeof(source_route->'binding_revision') = 'number' AND source_route->>'binding_revision' ~ '^[0-9]+$')) AND jsonb_typeof(source_route->'tenant_id') = 'string' AND jsonb_typeof(source_route->'bc_id') = 'string' AND jsonb_typeof(source_route->'connection_id') = 'string' AND jsonb_typeof(source_route->'channel') = 'string' AND jsonb_typeof(source_route->'authorization_revision') = 'number' AND jsonb_typeof(source_route->'adapter_contract_revision') = 'string' AND source_route->>'tenant_id' = tenant_id::text AND source_route->>'bc_id' = bc_id AND length(btrim(source_route->>'bc_id')) > 0 AND source_route->>'connection_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' AND source_route->>'channel' IN ('OFFICIAL_API','OFFICIAL_MCP') AND source_route->>'authorization_revision' ~ '^[0-9]+$' AND length(btrim(source_route->>'adapter_contract_revision')) > 0), false)),
	CONSTRAINT ck_material_share_batch_status CHECK (status IN ('pending','sending','acknowledged','result_unknown','not_sent','failed','completed')),
	CONSTRAINT ck_material_share_batch_materials CHECK (jsonb_typeof(material_ids) = 'array' AND jsonb_array_length(material_ids) <= 20 AND (wire_digest IS NULL OR jsonb_array_length(material_ids) >= 1)),
	CONSTRAINT ck_material_share_batch_targets CHECK (jsonb_typeof(advertiser_ids) = 'array' AND jsonb_array_length(advertiser_ids) BETWEEN 1 AND 10),
	CONSTRAINT ck_material_share_batch_digests CHECK (length(request_digest) = 64 AND (wire_digest IS NULL OR length(wire_digest) = 64)),
	FOREIGN KEY(actor_id) REFERENCES "user" (id)
)

;

CREATE TABLE material_share_batch_member (
	id UUID NOT NULL,
	tenant_id UUID NOT NULL,
	bc_id VARCHAR(128) NOT NULL,
	batch_id UUID NOT NULL,
	material_id UUID NOT NULL,
	advertiser_id VARCHAR(128) NOT NULL,
	distribution_id UUID NOT NULL,
	operation_id UUID NOT NULL,
	operation_claim UUID NOT NULL,
	operation_digest VARCHAR(64) NOT NULL,
	source_video_id VARCHAR(128) NOT NULL,
	source_evidence JSONB NOT NULL,
	source_mid VARCHAR(128),
	status VARCHAR NOT NULL,
	revision INTEGER NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(tenant_id, bc_id, batch_id) REFERENCES material_share_batch (tenant_id, bc_id, id),
	FOREIGN KEY(tenant_id, bc_id, material_id, advertiser_id, operation_id) REFERENCES material_asset_operation (tenant_id, bc_id, material_id, advertiser_id, id),
	CONSTRAINT uq_material_share_batch_operation UNIQUE (batch_id, operation_id),
	CONSTRAINT ck_material_share_member_status CHECK (status IN ('pending','verifying','result_unknown','not_sent','failed','ready')),
	CONSTRAINT ck_material_share_member_evidence CHECK (revision >= 0 AND length(operation_digest) = 64 AND jsonb_typeof(source_evidence) = 'object'),
	FOREIGN KEY(distribution_id) REFERENCES material_distribution (id)
)

;
CREATE INDEX ix_material_share_member_operation ON material_share_batch_member (tenant_id, operation_id);

CREATE TABLE material_share_batch_receipt (
	id UUID NOT NULL,
	tenant_id UUID NOT NULL,
	bc_id VARCHAR(128) NOT NULL,
	batch_id UUID NOT NULL,
	effect VARCHAR(32) NOT NULL,
	code VARCHAR(128),
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(tenant_id, bc_id, batch_id) REFERENCES material_share_batch (tenant_id, bc_id, id),
	CONSTRAINT ck_material_share_receipt_effect CHECK (effect IN ('ACKNOWLEDGED','UNKNOWN','NOT_SENT','FAILED'))
)

;
CREATE INDEX ix_material_share_receipt_batch ON material_share_batch_receipt (tenant_id, batch_id);

CREATE TABLE build_verified_replacement (
	id UUID NOT NULL,
	tenant_id UUID NOT NULL,
	bc_id VARCHAR(128) NOT NULL,
	submission_id UUID NOT NULL,
	preview_id UUID NOT NULL,
	advertiser_id VARCHAR(128) NOT NULL,
	request_id UUID NOT NULL,
	actor_id UUID NOT NULL,
	source_step_id UUID NOT NULL,
	source_group_step_id UUID NOT NULL,
	source_attempt INTEGER NOT NULL,
	source_attempt_id UUID NOT NULL,
	status VARCHAR NOT NULL,
	original_adgroup_id VARCHAR(255) NOT NULL,
	remote_adgroup_id VARCHAR(255) NOT NULL,
	remote_ad_id VARCHAR(255) NOT NULL,
	source_request_digest VARCHAR(64) NOT NULL,
	intent_digest VARCHAR(64) NOT NULL,
	request_digest VARCHAR(64) NOT NULL,
	audit_reference VARCHAR(1024) NOT NULL,
	route JSONB NOT NULL,
	original_intent JSONB NOT NULL,
	replacement_intent JSONB NOT NULL,
	verification JSONB NOT NULL,
	verified_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(tenant_id, submission_id, preview_id, bc_id) REFERENCES build_submission (tenant_id, id, preview_id, bc_id),
	FOREIGN KEY(tenant_id, submission_id, source_step_id) REFERENCES execution_step (tenant_id, submission_id, id),
	FOREIGN KEY(tenant_id, submission_id, source_group_step_id) REFERENCES execution_step (tenant_id, submission_id, id),
	FOREIGN KEY(tenant_id, source_step_id, source_attempt, source_attempt_id) REFERENCES build_attempt_context (tenant_id, step_id, attempt, attempt_id),
	CONSTRAINT uq_replacement_request UNIQUE (tenant_id, request_id),
	CONSTRAINT uq_replacement_source UNIQUE (tenant_id, source_step_id),
	CONSTRAINT uq_replacement_remote_ad UNIQUE (tenant_id, bc_id, advertiser_id, remote_ad_id),
	CONSTRAINT ck_replacement_status CHECK (status = 'VERIFIED_REPLACEMENT'),
	CONSTRAINT ck_replacement_digests CHECK (length(source_request_digest)=64 AND length(intent_digest)=64 AND length(request_digest)=64),
	CONSTRAINT ck_replacement_remote_ids CHECK (length(trim(remote_ad_id))>0 AND length(trim(remote_adgroup_id))>0 AND length(trim(original_adgroup_id))>0 AND remote_adgroup_id<>original_adgroup_id),
	FOREIGN KEY(actor_id) REFERENCES "user" (id)
)

;
CREATE INDEX ix_replacement_group ON build_verified_replacement (tenant_id, source_group_step_id);
CREATE INDEX ix_replacement_remote_group ON build_verified_replacement (tenant_id, bc_id, advertiser_id, remote_adgroup_id);
CREATE INDEX ix_replacement_submission ON build_verified_replacement (tenant_id, submission_id, source_step_id);

CREATE FUNCTION material_share_frozen_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'material share identity is immutable';
    END IF;
    IF TG_TABLE_NAME = 'material_share_batch' THEN
        IF (to_jsonb(NEW) - ARRAY['status','material_ids','wire_digest'])
             IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['status','material_ids','wire_digest'])
           OR ((NEW.material_ids IS DISTINCT FROM OLD.material_ids
                OR NEW.wire_digest IS DISTINCT FROM OLD.wire_digest)
               AND (OLD.status <> 'pending' OR OLD.wire_digest IS NOT NULL
                    OR OLD.material_ids <> '[]'::jsonb))
        THEN RAISE EXCEPTION 'material share batch identity is immutable'; END IF;
    ELSE
        IF (to_jsonb(NEW) - ARRAY['status','source_mid'])
             IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['status','source_mid'])
           OR (NEW.source_mid IS DISTINCT FROM OLD.source_mid
               AND (OLD.status <> 'pending' OR OLD.source_mid IS NOT NULL))
        THEN RAISE EXCEPTION 'material share member identity is immutable'; END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER material_share_batch_frozen BEFORE UPDATE OR DELETE ON material_share_batch
FOR EACH ROW EXECUTE FUNCTION material_share_frozen_guard();
CREATE TRIGGER material_share_member_frozen BEFORE UPDATE OR DELETE ON material_share_batch_member
FOR EACH ROW EXECUTE FUNCTION material_share_frozen_guard();
CREATE TRIGGER material_share_receipt_immutable BEFORE UPDATE OR DELETE ON material_share_batch_receipt
FOR EACH ROW EXECUTE FUNCTION build_execution_immutable();
CREATE TRIGGER build_replacement_immutable BEFORE UPDATE OR DELETE ON build_verified_replacement
FOR EACH ROW EXECUTE FUNCTION build_execution_immutable();
"""


def upgrade():
    op.execute(SCHEMA)
    # 按租户/账户就近取下一组，批量核验不重复排序整份万人队列。
    op.execute("""
        CREATE INDEX ix_cover_bulk_candidates ON material_cover_job
        (tenant_id, bc_id, actor_id, advertiser_id, connection_id, id)
        WHERE known_image_id IS NOT NULL AND status='VERIFYING'
    """)


def downgrade():
    # 不可逆移除已经产生的审计证据；空表时才允许回退。
    op.execute("""
        DO $$ BEGIN
            IF EXISTS(SELECT 1 FROM material_share_batch)
               OR EXISTS(SELECT 1 FROM build_verified_replacement)
            THEN RAISE EXCEPTION 'cannot downgrade retained batch/replacement evidence';
            END IF;
        END $$;
    """)
    op.drop_index("ix_cover_bulk_candidates", table_name="material_cover_job")
    for table in (
        "build_verified_replacement",
        "material_share_batch_receipt",
        "material_share_batch_member",
        "material_share_batch",
    ):
        op.drop_table(table)
    op.execute("DROP FUNCTION material_share_frozen_guard()")
