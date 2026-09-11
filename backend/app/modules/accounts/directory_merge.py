"""已完整验证的单 BC 暂存目录批量合并；调用者拥有整批原子事务。"""

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import Session

from app.core.errors import DomainError

from .models import DiscoveryRun, TenantBC


def merge_directory_bc(
    session: Session, *, run: DiscoveryRun, bc_id: str, bc_name: str
) -> None:
    from .discovery import claim_external_asset

    if not claim_external_asset(
        session, kind="BC", external_id=bc_id, tenant_id=run.tenant_id
    ):
        raise DomainError("account_ownership_conflict", "候选 BC 已由其他租户管理")
    params = {
        "run_id": run.id,
        "tenant_id": run.tenant_id,
        "connection_id": run.connection_id,
        "bc_id": bc_id,
    }
    # 由已验证暂存表直接批量合并；不逐账户 SQL，且整个授权切换只提交一次。
    session.execute(
        text("""
        INSERT INTO external_asset_owner (kind, external_id, owner_tenant_id)
        SELECT 'ADVERTISER', item->>'advertiser_id', :tenant_id
        FROM discovery_staged_page p CROSS JOIN LATERAL jsonb_array_elements(p.rows) item
        WHERE p.run_id=:run_id AND p.stage='ASSETS' AND p.bc_id=:bc_id
        ON CONFLICT (kind, external_id) DO NOTHING
    """),
        params,
    )
    conflict = session.execute(
        text("""
        SELECT EXISTS (SELECT 1 FROM discovery_staged_page p
        CROSS JOIN LATERAL jsonb_array_elements(p.rows) item
        JOIN external_asset_owner o ON o.kind='ADVERTISER' AND o.external_id=item->>'advertiser_id'
        WHERE p.run_id=:run_id AND p.stage='ASSETS' AND p.bc_id=:bc_id AND o.owner_tenant_id<>:tenant_id)
    """),
        params,
    ).scalar_one()
    if conflict:
        raise DomainError("account_ownership_conflict", "候选账户已由其他租户管理")
    session.exec(
        insert(TenantBC)
        .values(
            tenant_id=run.tenant_id,
            bc_id=bc_id,
            name=bc_name,
            ownership_conflict=False,
        )
        .on_conflict_do_update(
            index_elements=["tenant_id", "bc_id"],
            set_={"name": bc_name, "ownership_conflict": False},
        )
    )
    # 无授权详情只能建立未知的新账户，不能用缺失值覆盖其他连接已观察的共享元数据。
    session.execute(
        text("""
        WITH assets AS (SELECT item FROM discovery_staged_page p
            CROSS JOIN LATERAL jsonb_array_elements(p.rows) item WHERE p.run_id=:run_id AND p.stage='ASSETS' AND p.bc_id=:bc_id),
        details AS (SELECT item FROM discovery_staged_page p
            CROSS JOIN LATERAL jsonb_array_elements(p.rows) item WHERE p.run_id=:run_id AND p.stage='DETAILS' AND p.bc_id=:bc_id)
        INSERT INTO advertiser_account (tenant_id,advertiser_id,name,currency,timezone,remote_status,ownership_conflict)
        SELECT :tenant_id,a.item->>'advertiser_id',COALESCE(d.item->>'name',a.item->>'name',''),
            COALESCE(d.item->>'currency',''),COALESCE(d.item->>'timezone',''),COALESCE(d.item->>'remote_status','UNKNOWN'),false
        FROM assets a LEFT JOIN details d ON d.item->>'advertiser_id'=a.item->>'advertiser_id'
        ON CONFLICT (tenant_id,advertiser_id) DO NOTHING
    """),
        params,
    )
    session.execute(
        text("""
        UPDATE advertiser_account x SET name=item->>'name',currency=item->>'currency',
            timezone=item->>'timezone',remote_status=item->>'remote_status',ownership_conflict=false
        FROM discovery_staged_page p CROSS JOIN LATERAL jsonb_array_elements(p.rows) item
        WHERE p.run_id=:run_id AND p.stage='DETAILS' AND p.bc_id=:bc_id
            AND x.tenant_id=:tenant_id AND x.advertiser_id=item->>'advertiser_id'
    """),
        params,
    )
    session.execute(
        text("""
        WITH assets AS (SELECT item FROM discovery_staged_page p CROSS JOIN LATERAL jsonb_array_elements(p.rows) item
            WHERE p.run_id=:run_id AND p.stage='ASSETS' AND p.bc_id=:bc_id),
        authorized AS (SELECT item->>'advertiser_id' AS id FROM discovery_staged_page p
            CROSS JOIN LATERAL jsonb_array_elements(p.rows) item WHERE p.run_id=:run_id AND p.stage='AUTHORIZED')
        INSERT INTO bc_account_access (tenant_id,bc_id,advertiser_id,connection_id,in_bc,authorized,active,
            can_upload,can_build,permission_state,last_seen_run_id,checked_at)
        SELECT :tenant_id,:bc_id,a.item->>'advertiser_id',:connection_id,true,z.id IS NOT NULL,
            z.id IS NOT NULL AND x.currency<>'' AND x.timezone<>'' AND x.remote_status IN ('ENABLE','STATUS_ENABLE'),
            false,false,CASE WHEN x.currency='' OR x.timezone='' THEN 'METADATA_INCOMPLETE' ELSE 'UNKNOWN' END,
            :run_id,CURRENT_TIMESTAMP FROM assets a
        JOIN advertiser_account x ON x.tenant_id=:tenant_id AND x.advertiser_id=a.item->>'advertiser_id'
        LEFT JOIN authorized z ON z.id=a.item->>'advertiser_id'
        ON CONFLICT (tenant_id,bc_id,advertiser_id,connection_id) DO UPDATE SET
            in_bc=EXCLUDED.in_bc,authorized=EXCLUDED.authorized,active=EXCLUDED.active,
            can_upload=false,can_build=false,permission_state=EXCLUDED.permission_state,
            last_seen_run_id=EXCLUDED.last_seen_run_id,checked_at=EXCLUDED.checked_at
    """),
        params,
    )
