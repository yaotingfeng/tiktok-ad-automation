"""separate directory semantics from observations

Revision ID: mcp_directory_semantics
Revises: mcp02
Create Date: 2026-09-12 01:26:50.270088

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "mcp_directory_semantics"
down_revision = "mcp02"
branch_labels = None
depends_on = None


def _replace_grant_update(*, include_observation: bool) -> None:
    # 只替换目录 UPDATE 的比较元组，保留原有锁、成员增删、元数据及作用域隔离。
    facts = "tenant_id,bc_id,connection_id,advertiser_id,in_bc,authorized,active"
    if include_observation:
        facts += ",last_seen_run_id"
    op.execute(f"""
        CREATE OR REPLACE FUNCTION public.account_directory_grant_update() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $function$
        BEGIN
            INSERT INTO public.account_directory_revision AS fence
                (tenant_id,bc_id,connection_id,revision)
            SELECT touched.tenant_id,touched.bc_id,touched.connection_id,1
            FROM (
                SELECT DISTINCT tenant_id,bc_id,connection_id FROM (
                    (SELECT {facts} FROM new_rows EXCEPT SELECT {facts} FROM old_rows)
                    UNION
                    (SELECT {facts} FROM old_rows EXCEPT SELECT {facts} FROM new_rows)
                ) AS changed
            ) AS touched
            JOIN public.tenant_bc AS bc
              ON bc.tenant_id=touched.tenant_id AND bc.bc_id=touched.bc_id
            JOIN public.tiktok_connection AS connection
              ON connection.tenant_id=touched.tenant_id AND connection.id=touched.connection_id
            ORDER BY touched.tenant_id,touched.bc_id COLLATE "C",touched.connection_id
            ON CONFLICT (tenant_id,bc_id,connection_id)
                DO UPDATE SET revision=fence.revision+1;
            RETURN NULL;
        END;
        $function$;
    """)


def upgrade() -> None:
    # 检查时间、发现批次和令牌轮换是观察变化，不能废弃已冻结场景。
    op.execute("LOCK TABLE public.bc_account_access IN SHARE ROW EXCLUSIVE MODE")
    _replace_grant_update(include_observation=False)


def downgrade() -> None:
    op.execute("LOCK TABLE public.bc_account_access IN SHARE ROW EXCLUSIVE MODE")
    _replace_grant_update(include_observation=True)
