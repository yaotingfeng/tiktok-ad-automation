"""Share one MCP authorization across independently managed BC bindings.

Revision ID: mcp_multi_bc
Revises: mcp_cover_evidence
"""

import sqlalchemy as sa
from alembic import op

revision = "mcp_multi_bc"
down_revision = "mcp_cover_evidence"
branch_labels = None
depends_on = None


def _integer(table, name):
    op.add_column(
        table, sa.Column(name, sa.Integer(), nullable=False, server_default="0")
    )
    op.alter_column(table, name, server_default=None)


def _route_checks(values):
    for (table, name), expression in values.items():
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, expression)


def upgrade():
    op.drop_index("uq_mcp_one_bc", table_name="bc_connection_binding")
    op.add_column(
        "bc_connection_binding",
        sa.Column("status", sa.String(16), nullable=False, server_default="ACTIVE"),
    )
    op.alter_column("bc_connection_binding", "status", server_default=None)
    for name in ("authorization_revision", "revision"):
        _integer("bc_connection_binding", name)
    op.add_column(
        "bc_connection_binding",
        sa.Column("last_error_code", sa.String(128), nullable=True),
    )
    op.execute("""
        UPDATE bc_connection_binding b SET authorization_revision=c.authorization_revision
        FROM tiktok_connection c WHERE b.tenant_id=c.tenant_id AND b.connection_id=c.id
    """)
    op.create_check_constraint(
        "ck_bc_binding_lifecycle",
        "bc_connection_binding",
        "status IN ('SYNCING','ACTIVE','ERROR','DISABLED') AND authorization_revision >= 0 AND revision >= 0",
    )
    op.add_column("discovery_run", sa.Column("bc_id", sa.String(128), nullable=True))
    for name in ("authorization_revision", "binding_revision"):
        _integer("discovery_run", name)
    # 旧候选任务不能在新共享授权流程中继续消费令牌。保留旧回执，旧消息会正常被拒绝。
    op.execute("""
        UPDATE discovery_run SET status='ERROR', error_code='mcp_candidate_superseded',
            claim_id=NULL, claimed_until=NULL
        WHERE mcp_candidate_attempt_id IS NOT NULL AND status IN ('RUNNING','ADMISSION_WAIT')
    """)
    op.create_check_constraint(
        "ck_discovery_binding_revisions",
        "discovery_run",
        "authorization_revision >= 0 AND binding_revision >= 0",
    )
    op.drop_index("uq_discovery_active_connection", table_name="discovery_run")
    op.create_index(
        "uq_discovery_active_connection",
        "discovery_run",
        ["tenant_id", "connection_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('RUNNING','ADMISSION_WAIT') AND bc_id IS NULL"
        ),
    )
    op.create_index(
        "uq_discovery_active_bc",
        "discovery_run",
        ["tenant_id", "connection_id", "bc_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('RUNNING','ADMISSION_WAIT') AND bc_id IS NOT NULL"
        ),
    )
    _integer("build_route_context", "binding_revision")
    _integer("account_capability_job", "binding_revision")
    op.drop_index("uq_capability_active_basis", table_name="account_capability_job")
    op.create_index(
        "uq_capability_active_basis",
        "account_capability_job",
        [
            "tenant_id",
            "bc_id",
            "connection_id",
            "channel",
            "authorization_revision",
            "binding_revision",
            "adapter_contract_revision",
            "directory_basis",
        ],
        unique=True,
        postgresql_where=sa.text("status = 'PENDING'"),
    )
    # 历史不可变 JSON 不回写：缺字段固定解释为代数0，新路由显式存储非负整数。
    _route_checks(NEW_ROUTE_CHECKS)


def downgrade():
    # 不能丢弃多BC/解绑代数或新冻结证据。仅空接入环境允许回退结构。
    used = (
        op.get_bind()
        .execute(
            sa.text("""
        SELECT EXISTS(SELECT 1 FROM bc_connection_binding)
            OR EXISTS(SELECT 1 FROM discovery_run WHERE bc_id IS NOT NULL)
            OR EXISTS(SELECT 1 FROM build_route_context WHERE binding_revision<>0)
            OR EXISTS(SELECT 1 FROM account_capability_job WHERE binding_revision<>0)
    """)
        )
        .scalar_one()
    )
    if used:
        raise RuntimeError("BC authorization and binding evidence cannot be downgraded")
    _route_checks(OLD_ROUTE_CHECKS)
    op.drop_index("uq_capability_active_basis", table_name="account_capability_job")
    op.create_index(
        "uq_capability_active_basis",
        "account_capability_job",
        [
            "tenant_id",
            "bc_id",
            "connection_id",
            "channel",
            "authorization_revision",
            "adapter_contract_revision",
            "directory_basis",
        ],
        unique=True,
        postgresql_where=sa.text("status = 'PENDING'"),
    )
    op.drop_column("account_capability_job", "binding_revision")
    op.drop_column("build_route_context", "binding_revision")
    op.drop_index("uq_discovery_active_bc", table_name="discovery_run")
    op.drop_index("uq_discovery_active_connection", table_name="discovery_run")
    op.create_index(
        "uq_discovery_active_connection",
        "discovery_run",
        ["tenant_id", "connection_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('RUNNING','ADMISSION_WAIT')"),
    )
    op.drop_constraint("ck_discovery_binding_revisions", "discovery_run", type_="check")
    for name in ("bc_id", "authorization_revision", "binding_revision"):
        op.drop_column("discovery_run", name)
    op.drop_constraint(
        "ck_bc_binding_lifecycle", "bc_connection_binding", type_="check"
    )
    for name in ("status", "authorization_revision", "revision", "last_error_code"):
        op.drop_column("bc_connection_binding", name)
    op.create_index(
        "uq_mcp_one_bc",
        "bc_connection_binding",
        ["tenant_id", "connection_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'OFFICIAL_MCP'"),
    )


NEW_ROUTE_CHECKS = {
    (
        "upload_batch",
        "ck_upload_batch_frozen_route",
    ): "frozen_route IS NULL OR COALESCE((jsonb_typeof(frozen_route) = 'object' AND frozen_route ?& ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision'] AND frozen_route - ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision'] - 'binding_revision' = '{}'::jsonb AND (NOT frozen_route ? 'binding_revision' OR (jsonb_typeof(frozen_route->'binding_revision') = 'number' AND frozen_route->>'binding_revision' ~ '^[0-9]+$')) AND jsonb_typeof(frozen_route->'tenant_id') = 'string' AND jsonb_typeof(frozen_route->'bc_id') = 'string' AND jsonb_typeof(frozen_route->'connection_id') = 'string' AND jsonb_typeof(frozen_route->'channel') = 'string' AND jsonb_typeof(frozen_route->'authorization_revision') = 'number' AND jsonb_typeof(frozen_route->'adapter_contract_revision') = 'string' AND frozen_route->>'tenant_id' = tenant_id::text AND frozen_route->>'bc_id' = bc_id AND length(btrim(frozen_route->>'bc_id')) > 0 AND frozen_route->>'connection_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' AND frozen_route->>'channel' IN ('OFFICIAL_API','OFFICIAL_MCP') AND frozen_route->>'authorization_revision' ~ '^[0-9]+$' AND length(btrim(frozen_route->>'adapter_contract_revision')) > 0), false)",
    (
        "material_asset_operation",
        "ck_material_asset_operation_frozen_route",
    ): "frozen_route IS NULL OR COALESCE((jsonb_typeof(frozen_route) = 'object' AND frozen_route ?& ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision'] AND frozen_route - ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision'] - 'binding_revision' = '{}'::jsonb AND (NOT frozen_route ? 'binding_revision' OR (jsonb_typeof(frozen_route->'binding_revision') = 'number' AND frozen_route->>'binding_revision' ~ '^[0-9]+$')) AND jsonb_typeof(frozen_route->'tenant_id') = 'string' AND jsonb_typeof(frozen_route->'bc_id') = 'string' AND jsonb_typeof(frozen_route->'connection_id') = 'string' AND jsonb_typeof(frozen_route->'channel') = 'string' AND jsonb_typeof(frozen_route->'authorization_revision') = 'number' AND jsonb_typeof(frozen_route->'adapter_contract_revision') = 'string' AND frozen_route->>'tenant_id' = tenant_id::text AND frozen_route->>'bc_id' = bc_id AND length(btrim(frozen_route->>'bc_id')) > 0 AND frozen_route->>'connection_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' AND frozen_route->>'channel' IN ('OFFICIAL_API','OFFICIAL_MCP') AND frozen_route->>'authorization_revision' ~ '^[0-9]+$' AND length(btrim(frozen_route->>'adapter_contract_revision')) > 0), false)",
    (
        "material_distribution",
        "ck_material_distribution_target_route",
    ): "target_route IS NULL OR COALESCE((jsonb_typeof(target_route) = 'object' AND target_route ?& ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision'] AND target_route - ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision'] - 'binding_revision' = '{}'::jsonb AND (NOT target_route ? 'binding_revision' OR (jsonb_typeof(target_route->'binding_revision') = 'number' AND target_route->>'binding_revision' ~ '^[0-9]+$')) AND jsonb_typeof(target_route->'tenant_id') = 'string' AND jsonb_typeof(target_route->'bc_id') = 'string' AND jsonb_typeof(target_route->'connection_id') = 'string' AND jsonb_typeof(target_route->'channel') = 'string' AND jsonb_typeof(target_route->'authorization_revision') = 'number' AND jsonb_typeof(target_route->'adapter_contract_revision') = 'string' AND target_route->>'tenant_id' = tenant_id::text AND target_route->>'bc_id' = bc_id AND length(btrim(target_route->>'bc_id')) > 0 AND target_route->>'connection_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' AND target_route->>'channel' IN ('OFFICIAL_API','OFFICIAL_MCP') AND target_route->>'authorization_revision' ~ '^[0-9]+$' AND length(btrim(target_route->>'adapter_contract_revision')) > 0), false)",
    (
        "material_distribution",
        "ck_material_distribution_source_route",
    ): "source_route IS NULL OR COALESCE((jsonb_typeof(source_route) = 'object' AND source_route ?& ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision'] AND source_route - ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision'] - 'binding_revision' = '{}'::jsonb AND (NOT source_route ? 'binding_revision' OR (jsonb_typeof(source_route->'binding_revision') = 'number' AND source_route->>'binding_revision' ~ '^[0-9]+$')) AND jsonb_typeof(source_route->'tenant_id') = 'string' AND jsonb_typeof(source_route->'bc_id') = 'string' AND jsonb_typeof(source_route->'connection_id') = 'string' AND jsonb_typeof(source_route->'channel') = 'string' AND jsonb_typeof(source_route->'authorization_revision') = 'number' AND jsonb_typeof(source_route->'adapter_contract_revision') = 'string' AND source_route->>'tenant_id' = tenant_id::text AND source_route->>'bc_id' = bc_id AND length(btrim(source_route->>'bc_id')) > 0 AND source_route->>'connection_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' AND source_route->>'channel' IN ('OFFICIAL_API','OFFICIAL_MCP') AND source_route->>'authorization_revision' ~ '^[0-9]+$' AND length(btrim(source_route->>'adapter_contract_revision')) > 0), false)",
    (
        "material_cover_job",
        "ck_material_cover_job_frozen_route",
    ): "frozen_route IS NULL OR COALESCE((jsonb_typeof(frozen_route) = 'object' AND frozen_route ?& ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision'] AND frozen_route - ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision'] - 'binding_revision' = '{}'::jsonb AND (NOT frozen_route ? 'binding_revision' OR (jsonb_typeof(frozen_route->'binding_revision') = 'number' AND frozen_route->>'binding_revision' ~ '^[0-9]+$')) AND jsonb_typeof(frozen_route->'tenant_id') = 'string' AND jsonb_typeof(frozen_route->'bc_id') = 'string' AND jsonb_typeof(frozen_route->'connection_id') = 'string' AND jsonb_typeof(frozen_route->'channel') = 'string' AND jsonb_typeof(frozen_route->'authorization_revision') = 'number' AND jsonb_typeof(frozen_route->'adapter_contract_revision') = 'string' AND frozen_route->>'tenant_id' = tenant_id::text AND frozen_route->>'bc_id' = bc_id AND length(btrim(frozen_route->>'bc_id')) > 0 AND frozen_route->>'connection_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' AND frozen_route->>'channel' IN ('OFFICIAL_API','OFFICIAL_MCP') AND frozen_route->>'authorization_revision' ~ '^[0-9]+$' AND length(btrim(frozen_route->>'adapter_contract_revision')) > 0 AND frozen_route->>'connection_id' = connection_id::text), false)",
    (
        "ingest_session",
        "ck_ingest_session_frozen_route",
    ): "frozen_route IS NULL OR COALESCE((jsonb_typeof(frozen_route) = 'object' AND frozen_route ?& ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision'] AND frozen_route - ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision'] - 'binding_revision' = '{}'::jsonb AND (NOT frozen_route ? 'binding_revision' OR (jsonb_typeof(frozen_route->'binding_revision') = 'number' AND frozen_route->>'binding_revision' ~ '^[0-9]+$')) AND jsonb_typeof(frozen_route->'tenant_id') = 'string' AND jsonb_typeof(frozen_route->'bc_id') = 'string' AND jsonb_typeof(frozen_route->'connection_id') = 'string' AND jsonb_typeof(frozen_route->'channel') = 'string' AND jsonb_typeof(frozen_route->'authorization_revision') = 'number' AND jsonb_typeof(frozen_route->'adapter_contract_revision') = 'string' AND frozen_route->>'tenant_id' = tenant_id::text AND frozen_route->>'bc_id' = bc_id AND length(btrim(frozen_route->>'bc_id')) > 0 AND frozen_route->>'connection_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' AND frozen_route->>'channel' IN ('OFFICIAL_API','OFFICIAL_MCP') AND frozen_route->>'authorization_revision' ~ '^[0-9]+$' AND length(btrim(frozen_route->>'adapter_contract_revision')) > 0), false)",
    (
        "build_scene_job",
        "ck_build_scene_job_frozen_route",
    ): "frozen_route IS NULL OR COALESCE((\njsonb_typeof(frozen_route) = 'object'\nAND frozen_route ?& ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision']\nAND frozen_route - ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision','binding_revision'] = '{}'::jsonb\nAND (NOT frozen_route ? 'binding_revision' OR (jsonb_typeof(frozen_route->'binding_revision') = 'number' AND frozen_route->>'binding_revision' ~ '^[0-9]+$'))\nAND jsonb_typeof(frozen_route->'tenant_id') = 'string'\nAND jsonb_typeof(frozen_route->'bc_id') = 'string'\nAND jsonb_typeof(frozen_route->'connection_id') = 'string'\nAND jsonb_typeof(frozen_route->'channel') = 'string'\nAND jsonb_typeof(frozen_route->'authorization_revision') = 'number'\nAND jsonb_typeof(frozen_route->'adapter_contract_revision') = 'string'\nAND frozen_route->>'tenant_id' = tenant_id::text\nAND frozen_route->>'connection_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'\nAND length(btrim(frozen_route->>'bc_id')) > 0\nAND frozen_route->>'channel' IN ('OFFICIAL_API','OFFICIAL_MCP')\nAND frozen_route->>'authorization_revision' ~ '^[0-9]+$'\nAND length(btrim(frozen_route->>'adapter_contract_revision')) > 0\n AND frozen_route->>'bc_id' = bc_id AND frozen_route->>'connection_id' = connection_id::text), false)",
    (
        "draft_scene_preparation",
        "ck_draft_scene_preparation_frozen_route",
    ): "frozen_route IS NULL OR COALESCE((\njsonb_typeof(frozen_route) = 'object'\nAND frozen_route ?& ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision']\nAND frozen_route - ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision','binding_revision'] = '{}'::jsonb\nAND (NOT frozen_route ? 'binding_revision' OR (jsonb_typeof(frozen_route->'binding_revision') = 'number' AND frozen_route->>'binding_revision' ~ '^[0-9]+$'))\nAND jsonb_typeof(frozen_route->'tenant_id') = 'string'\nAND jsonb_typeof(frozen_route->'bc_id') = 'string'\nAND jsonb_typeof(frozen_route->'connection_id') = 'string'\nAND jsonb_typeof(frozen_route->'channel') = 'string'\nAND jsonb_typeof(frozen_route->'authorization_revision') = 'number'\nAND jsonb_typeof(frozen_route->'adapter_contract_revision') = 'string'\nAND frozen_route->>'tenant_id' = tenant_id::text\nAND frozen_route->>'connection_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'\nAND length(btrim(frozen_route->>'bc_id')) > 0\nAND frozen_route->>'channel' IN ('OFFICIAL_API','OFFICIAL_MCP')\nAND frozen_route->>'authorization_revision' ~ '^[0-9]+$'\nAND length(btrim(frozen_route->>'adapter_contract_revision')) > 0\n), false)",
    (
        "build_historical_read",
        "ck_historical_read_route_shape",
    ): "coalesce((jsonb_typeof(old_route)='object' AND old_route ?& ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision'] AND old_route - ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision'] - 'binding_revision' = '{}'::jsonb AND (NOT old_route ? 'binding_revision' OR (jsonb_typeof(old_route->'binding_revision')='number' AND old_route->>'binding_revision' ~ '^[0-9]+$')) AND jsonb_typeof(old_route->'tenant_id')='string' AND jsonb_typeof(old_route->'bc_id')='string' AND jsonb_typeof(old_route->'connection_id')='string' AND jsonb_typeof(old_route->'channel')='string' AND jsonb_typeof(old_route->'adapter_contract_revision')='string' AND jsonb_typeof(old_route->'authorization_revision')='number' AND old_route->>'authorization_revision' ~ '^[0-9]+$' AND jsonb_typeof(new_route)='object' AND new_route ?& ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision'] AND new_route - ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision'] - 'binding_revision' = '{}'::jsonb AND (NOT new_route ? 'binding_revision' OR (jsonb_typeof(new_route->'binding_revision')='number' AND new_route->>'binding_revision' ~ '^[0-9]+$')) AND jsonb_typeof(new_route->'tenant_id')='string' AND jsonb_typeof(new_route->'bc_id')='string' AND jsonb_typeof(new_route->'connection_id')='string' AND jsonb_typeof(new_route->'channel')='string' AND jsonb_typeof(new_route->'adapter_contract_revision')='string' AND jsonb_typeof(new_route->'authorization_revision')='number' AND new_route->>'authorization_revision' ~ '^[0-9]+$'),false)",
}
OLD_ROUTE_CHECKS = {
    (
        "upload_batch",
        "ck_upload_batch_frozen_route",
    ): "frozen_route IS NULL OR COALESCE((jsonb_typeof(frozen_route) = 'object' AND frozen_route ?& ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision'] AND frozen_route - ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision'] = '{}'::jsonb AND jsonb_typeof(frozen_route->'tenant_id') = 'string' AND jsonb_typeof(frozen_route->'bc_id') = 'string' AND jsonb_typeof(frozen_route->'connection_id') = 'string' AND jsonb_typeof(frozen_route->'channel') = 'string' AND jsonb_typeof(frozen_route->'authorization_revision') = 'number' AND jsonb_typeof(frozen_route->'adapter_contract_revision') = 'string' AND frozen_route->>'tenant_id' = tenant_id::text AND frozen_route->>'bc_id' = bc_id AND length(btrim(frozen_route->>'bc_id')) > 0 AND frozen_route->>'connection_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' AND frozen_route->>'channel' IN ('OFFICIAL_API','OFFICIAL_MCP') AND frozen_route->>'authorization_revision' ~ '^[0-9]+$' AND length(btrim(frozen_route->>'adapter_contract_revision')) > 0), false)",
    (
        "material_asset_operation",
        "ck_material_asset_operation_frozen_route",
    ): "frozen_route IS NULL OR COALESCE((jsonb_typeof(frozen_route) = 'object' AND frozen_route ?& ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision'] AND frozen_route - ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision'] = '{}'::jsonb AND jsonb_typeof(frozen_route->'tenant_id') = 'string' AND jsonb_typeof(frozen_route->'bc_id') = 'string' AND jsonb_typeof(frozen_route->'connection_id') = 'string' AND jsonb_typeof(frozen_route->'channel') = 'string' AND jsonb_typeof(frozen_route->'authorization_revision') = 'number' AND jsonb_typeof(frozen_route->'adapter_contract_revision') = 'string' AND frozen_route->>'tenant_id' = tenant_id::text AND frozen_route->>'bc_id' = bc_id AND length(btrim(frozen_route->>'bc_id')) > 0 AND frozen_route->>'connection_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' AND frozen_route->>'channel' IN ('OFFICIAL_API','OFFICIAL_MCP') AND frozen_route->>'authorization_revision' ~ '^[0-9]+$' AND length(btrim(frozen_route->>'adapter_contract_revision')) > 0), false)",
    (
        "material_distribution",
        "ck_material_distribution_target_route",
    ): "target_route IS NULL OR COALESCE((jsonb_typeof(target_route) = 'object' AND target_route ?& ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision'] AND target_route - ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision'] = '{}'::jsonb AND jsonb_typeof(target_route->'tenant_id') = 'string' AND jsonb_typeof(target_route->'bc_id') = 'string' AND jsonb_typeof(target_route->'connection_id') = 'string' AND jsonb_typeof(target_route->'channel') = 'string' AND jsonb_typeof(target_route->'authorization_revision') = 'number' AND jsonb_typeof(target_route->'adapter_contract_revision') = 'string' AND target_route->>'tenant_id' = tenant_id::text AND target_route->>'bc_id' = bc_id AND length(btrim(target_route->>'bc_id')) > 0 AND target_route->>'connection_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' AND target_route->>'channel' IN ('OFFICIAL_API','OFFICIAL_MCP') AND target_route->>'authorization_revision' ~ '^[0-9]+$' AND length(btrim(target_route->>'adapter_contract_revision')) > 0), false)",
    (
        "material_distribution",
        "ck_material_distribution_source_route",
    ): "source_route IS NULL OR COALESCE((jsonb_typeof(source_route) = 'object' AND source_route ?& ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision'] AND source_route - ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision'] = '{}'::jsonb AND jsonb_typeof(source_route->'tenant_id') = 'string' AND jsonb_typeof(source_route->'bc_id') = 'string' AND jsonb_typeof(source_route->'connection_id') = 'string' AND jsonb_typeof(source_route->'channel') = 'string' AND jsonb_typeof(source_route->'authorization_revision') = 'number' AND jsonb_typeof(source_route->'adapter_contract_revision') = 'string' AND source_route->>'tenant_id' = tenant_id::text AND source_route->>'bc_id' = bc_id AND length(btrim(source_route->>'bc_id')) > 0 AND source_route->>'connection_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' AND source_route->>'channel' IN ('OFFICIAL_API','OFFICIAL_MCP') AND source_route->>'authorization_revision' ~ '^[0-9]+$' AND length(btrim(source_route->>'adapter_contract_revision')) > 0), false)",
    (
        "material_cover_job",
        "ck_material_cover_job_frozen_route",
    ): "frozen_route IS NULL OR COALESCE((jsonb_typeof(frozen_route) = 'object' AND frozen_route ?& ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision'] AND frozen_route - ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision'] = '{}'::jsonb AND jsonb_typeof(frozen_route->'tenant_id') = 'string' AND jsonb_typeof(frozen_route->'bc_id') = 'string' AND jsonb_typeof(frozen_route->'connection_id') = 'string' AND jsonb_typeof(frozen_route->'channel') = 'string' AND jsonb_typeof(frozen_route->'authorization_revision') = 'number' AND jsonb_typeof(frozen_route->'adapter_contract_revision') = 'string' AND frozen_route->>'tenant_id' = tenant_id::text AND frozen_route->>'bc_id' = bc_id AND length(btrim(frozen_route->>'bc_id')) > 0 AND frozen_route->>'connection_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' AND frozen_route->>'channel' IN ('OFFICIAL_API','OFFICIAL_MCP') AND frozen_route->>'authorization_revision' ~ '^[0-9]+$' AND length(btrim(frozen_route->>'adapter_contract_revision')) > 0 AND frozen_route->>'connection_id' = connection_id::text), false)",
    (
        "ingest_session",
        "ck_ingest_session_frozen_route",
    ): "frozen_route IS NULL OR COALESCE((jsonb_typeof(frozen_route) = 'object' AND frozen_route ?& ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision'] AND frozen_route - ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision'] = '{}'::jsonb AND jsonb_typeof(frozen_route->'tenant_id') = 'string' AND jsonb_typeof(frozen_route->'bc_id') = 'string' AND jsonb_typeof(frozen_route->'connection_id') = 'string' AND jsonb_typeof(frozen_route->'channel') = 'string' AND jsonb_typeof(frozen_route->'authorization_revision') = 'number' AND jsonb_typeof(frozen_route->'adapter_contract_revision') = 'string' AND frozen_route->>'tenant_id' = tenant_id::text AND frozen_route->>'bc_id' = bc_id AND length(btrim(frozen_route->>'bc_id')) > 0 AND frozen_route->>'connection_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' AND frozen_route->>'channel' IN ('OFFICIAL_API','OFFICIAL_MCP') AND frozen_route->>'authorization_revision' ~ '^[0-9]+$' AND length(btrim(frozen_route->>'adapter_contract_revision')) > 0), false)",
    (
        "build_scene_job",
        "ck_build_scene_job_frozen_route",
    ): "frozen_route IS NULL OR COALESCE((\njsonb_typeof(frozen_route) = 'object'\nAND frozen_route ?& ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision']\nAND frozen_route - ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision'] = '{}'::jsonb\nAND jsonb_typeof(frozen_route->'tenant_id') = 'string'\nAND jsonb_typeof(frozen_route->'bc_id') = 'string'\nAND jsonb_typeof(frozen_route->'connection_id') = 'string'\nAND jsonb_typeof(frozen_route->'channel') = 'string'\nAND jsonb_typeof(frozen_route->'authorization_revision') = 'number'\nAND jsonb_typeof(frozen_route->'adapter_contract_revision') = 'string'\nAND frozen_route->>'tenant_id' = tenant_id::text\nAND frozen_route->>'connection_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'\nAND length(btrim(frozen_route->>'bc_id')) > 0\nAND frozen_route->>'channel' IN ('OFFICIAL_API','OFFICIAL_MCP')\nAND frozen_route->>'authorization_revision' ~ '^[0-9]+$'\nAND length(btrim(frozen_route->>'adapter_contract_revision')) > 0\n AND frozen_route->>'bc_id' = bc_id AND frozen_route->>'connection_id' = connection_id::text), false)",
    (
        "draft_scene_preparation",
        "ck_draft_scene_preparation_frozen_route",
    ): "frozen_route IS NULL OR COALESCE((\njsonb_typeof(frozen_route) = 'object'\nAND frozen_route ?& ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision']\nAND frozen_route - ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision'] = '{}'::jsonb\nAND jsonb_typeof(frozen_route->'tenant_id') = 'string'\nAND jsonb_typeof(frozen_route->'bc_id') = 'string'\nAND jsonb_typeof(frozen_route->'connection_id') = 'string'\nAND jsonb_typeof(frozen_route->'channel') = 'string'\nAND jsonb_typeof(frozen_route->'authorization_revision') = 'number'\nAND jsonb_typeof(frozen_route->'adapter_contract_revision') = 'string'\nAND frozen_route->>'tenant_id' = tenant_id::text\nAND frozen_route->>'connection_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'\nAND length(btrim(frozen_route->>'bc_id')) > 0\nAND frozen_route->>'channel' IN ('OFFICIAL_API','OFFICIAL_MCP')\nAND frozen_route->>'authorization_revision' ~ '^[0-9]+$'\nAND length(btrim(frozen_route->>'adapter_contract_revision')) > 0\n), false)",
    (
        "build_historical_read",
        "ck_historical_read_route_shape",
    ): "coalesce((jsonb_typeof(old_route)='object' AND old_route ?& ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision'] AND old_route - ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision'] = '{}'::jsonb AND jsonb_typeof(old_route->'tenant_id')='string' AND jsonb_typeof(old_route->'bc_id')='string' AND jsonb_typeof(old_route->'connection_id')='string' AND jsonb_typeof(old_route->'channel')='string' AND jsonb_typeof(old_route->'adapter_contract_revision')='string' AND jsonb_typeof(old_route->'authorization_revision')='number' AND old_route->>'authorization_revision' ~ '^[0-9]+$' AND jsonb_typeof(new_route)='object' AND new_route ?& ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision'] AND new_route - ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision'] = '{}'::jsonb AND jsonb_typeof(new_route->'tenant_id')='string' AND jsonb_typeof(new_route->'bc_id')='string' AND jsonb_typeof(new_route->'connection_id')='string' AND jsonb_typeof(new_route->'channel')='string' AND jsonb_typeof(new_route->'adapter_contract_revision')='string' AND jsonb_typeof(new_route->'authorization_revision')='number' AND new_route->>'authorization_revision' ~ '^[0-9]+$'),false)",
}
