"""Preserve the frozen scope of pre-multibc MCP runtime observations.

Revision ID: mcp_multibc_runtime
Revises: mcp_multi_bc
"""

from alembic import op

revision = "mcp_multibc_runtime"
down_revision = "mcp_multi_bc"
branch_labels = None
depends_on = None


def upgrade():
    # 旧版在目录发布时才接受授权，却未记录接受时间；由原完成回执补齐现有事实。
    op.execute("""
        UPDATE mcp_authorization_attempt a SET completed_at=p.completed_at
        FROM (SELECT tenant_id, connection_id, mcp_candidate_attempt_id AS attempt_id,
                max(completed_at) AS completed_at
            FROM discovery_run WHERE mcp_candidate_attempt_id IS NOT NULL
                AND status='COMPLETE' AND completed_at IS NOT NULL
            GROUP BY tenant_id, connection_id, mcp_candidate_attempt_id) p
        WHERE a.tenant_id=p.tenant_id AND a.connection_id=p.connection_id
            AND a.id=p.attempt_id AND a.status='ACCEPTED'
            AND (a.completed_at IS NULL OR a.completed_at<p.completed_at)
    """)
    # 只能从旧任务自己的冻结证据取版本，不能拿今日绑定代数替换历史授权。
    op.execute("""
        WITH frozen AS (
            SELECT r.id, r.work->'route'->>'bc_id' AS bc,
                CASE WHEN r.work->'route'->>'authorization_revision' ~ '^[0-9]{1,10}$'
                    THEN (r.work->'route'->>'authorization_revision')::numeric END AS auth,
                CASE WHEN NOT ((r.work->'route')::jsonb ? 'binding_revision') THEN 0
                    WHEN r.work->'route'->>'binding_revision' ~ '^[0-9]{1,10}$'
                    THEN (r.work->'route'->>'binding_revision')::numeric END AS binding
            FROM discovery_run r JOIN tiktok_connection c
                ON c.tenant_id=r.tenant_id AND c.id=r.connection_id
            WHERE c.kind='OFFICIAL_MCP' AND r.bc_id IS NULL
                AND r.work->>'mode'='RUNTIME_REFRESH'
                AND json_typeof(r.work->'route')='object'
                AND (r.work->'route')::jsonb ?& ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision']
                AND (r.work->'route')::jsonb - ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision','binding_revision']='{}'::jsonb
                AND json_typeof(r.work->'route'->'authorization_revision')='number'
                AND (NOT ((r.work->'route')::jsonb ? 'binding_revision') OR json_typeof(r.work->'route'->'binding_revision')='number')
                AND json_typeof(r.work->'route'->'bc_id')='string'
                AND json_typeof(r.work->'route'->'adapter_contract_revision')='string'
                AND length(btrim(r.work->'route'->>'adapter_contract_revision'))>0
                AND length(btrim(r.work->'route'->>'bc_id'))>0
                AND r.work->'route'->>'tenant_id'=r.tenant_id::text
                AND r.work->'route'->>'connection_id'=r.connection_id::text
                AND r.work->'route'->>'channel'='OFFICIAL_MCP'
                AND r.work->'route'->>'bc_id'=r.work->>'bc_id'
                AND length(r.work->'route'->>'bc_id') BETWEEN 1 AND 128
        ), valid AS (
            SELECT id, bc,
                CASE WHEN auth BETWEEN 0 AND 2147483647 THEN auth::integer END AS auth,
                CASE WHEN binding BETWEEN 0 AND 2147483647 THEN binding::integer END AS binding
            FROM frozen
        )
        UPDATE discovery_run r SET bc_id=v.bc, authorization_revision=v.auth,
            binding_revision=v.binding
        FROM valid v WHERE r.id=v.id AND v.auth IS NOT NULL AND v.binding IS NOT NULL
    """)
    # 无法证明作用域的旧未完成任务保留证据并终止，等待正常任务恢复重新观察。
    op.execute("""
        UPDATE discovery_run r SET status='ERROR', error_code='discovery_stale',
            claim_id=NULL, claimed_until=NULL
        FROM tiktok_connection c WHERE c.tenant_id=r.tenant_id AND c.id=r.connection_id
            AND c.kind='OFFICIAL_MCP' AND r.bc_id IS NULL
            AND r.work->>'mode'='RUNTIME_REFRESH'
            AND r.status IN ('RUNNING','ADMISSION_WAIT')
    """)


def downgrade():
    # 作用域是已有冻结证据的结构化副本，回退不得删除证据或复活失效任务。
    pass
