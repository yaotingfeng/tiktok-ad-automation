"""完整目录发布复用已读取的角色页；授权接入后无需第二次能力查询。"""

from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session as SASession
from sqlmodel import Session

from app.core.config import settings
from app.core.context import TenantContext
from app.integrations.tiktok.contracts.accounts import AuthorizationFacts

from .capabilities import _directory_basis, _scope_flags
from .capability_models import CapabilityJob
from .models import DiscoveryRun, TikTokConnection
from .routing import freeze_route


def publish_directory_capabilities(
    session: Session,
    *,
    context: TenantContext,
    run: DiscoveryRun,
    connection: TikTokConnection,
    bc_id: str,
    facts: AuthorizationFacts,
) -> None:
    """仅由已完成全分页、范围与新鲜度校验的目录发布事务调用。"""
    route = freeze_route(
        session, context=context, bc_id=bc_id, connection_id=connection.id
    )
    basis = _directory_basis(session, context, bc_id, connection.id)
    assert basis is not None
    known, build, upload = _scope_flags(facts)
    job = CapabilityJob(
        tenant_id=context.tenant_id,
        bc_id=bc_id,
        connection_id=connection.id,
        actor_id=context.actor_id,
        credential_revision=connection.credential_revision,
        channel=route.channel,
        authorization_revision=route.authorization_revision,
        binding_revision=route.binding_revision,
        adapter_contract_revision=route.adapter_contract_revision,
        directory_basis=basis,
        status="COMPLETE",
        phase="DONE",
        scope_known=known,
        scope_build=build,
        scope_upload=upload,
        completed_at=datetime.now(UTC),
        expires_at=facts.observed_at
        + timedelta(seconds=settings.BC_CAPABILITY_MAX_AGE_SECONDS),
    )
    session.add(job)
    session.flush()
    params = {
        "job_id": job.id,
        "run_id": run.id,
        "tenant_id": context.tenant_id,
        "bc_id": bc_id,
        "connection_id": connection.id,
        "known": known,
        "build": build,
        "upload": upload,
    }
    SASession.execute(
        session,
        text("""
        INSERT INTO account_capability_page (job_id,page,tenant_id,bc_id,row_count,observed_at)
        SELECT :job_id,page,tenant_id,bc_id,jsonb_array_length(rows),observed_at
        FROM discovery_staged_page WHERE run_id=:run_id AND bc_id=:bc_id AND stage='ROLES'
    """),
        params,
    )
    SASession.execute(
        session,
        text("""
        INSERT INTO account_capability_asset (job_id,advertiser_id,tenant_id,bc_id,page,role)
        SELECT :job_id,item->>'advertiser_id',p.tenant_id,p.bc_id,p.page,item->>'role'
        FROM discovery_staged_page p CROSS JOIN LATERAL jsonb_array_elements(p.rows) item
        WHERE run_id=:run_id AND bc_id=:bc_id AND stage='ROLES' AND item->>'role' IS NOT NULL
    """),
        params,
    )
    # 缺角色、只读角色、缺元数据和非正常账户仍不能写。每个 BC 一次 SQL，无逐账户循环。
    # 账户与角色按目标行的完整主键逐行取值，不做表间关联：本事务刚写入的账户、
    # 授权与角色行不在统计信息里（生产统计为本租户 0 行），关联会被规划成只按
    # tenant_id 扫索引的嵌套循环，近 5 千账户的 BC 单条耗时 13 秒，发布反复超时回滚。
    # 外键保证每条授权行都有对应账户，逐行取值覆盖的行与原关联一致。
    result = SASession.execute(
        session,
        text("""
        UPDATE bc_account_access g SET (can_upload, can_build, permission_state) = (
          SELECT COALESCE(c.known AND c.operate AND :upload,false),
            COALESCE(c.known AND c.operate AND :build,false),
            CASE WHEN c.known THEN 'VERIFIED'
              WHEN g.permission_state='METADATA_INCOMPLETE' THEN 'METADATA_INCOMPLETE' ELSE 'UNKNOWN' END
          FROM advertiser_account x
          LEFT JOIN account_capability_asset a ON a.job_id=:job_id AND a.advertiser_id=g.advertiser_id
          CROSS JOIN LATERAL (SELECT
              :known AND a.role IS NOT NULL AND g.in_bc AND g.authorized AND g.active
                AND NOT x.ownership_conflict AND x.currency<>'' AND x.timezone<>'' AS known,
              a.role IN ('ADMIN','OPERATOR') AND x.remote_status IN ('ENABLE','STATUS_ENABLE') AS operate
          ) c
          WHERE x.tenant_id=g.tenant_id AND x.advertiser_id=g.advertiser_id
        )
        WHERE g.tenant_id=:tenant_id AND g.bc_id=:bc_id AND g.connection_id=:connection_id
    """),
        params,
    )
    job.published_count = cast(CursorResult, result).rowcount
    pages, count = SASession.execute(
        session,
        text("""
        SELECT count(*),COALESCE(sum(row_count),0) FROM account_capability_page WHERE job_id=:job_id
    """),
        params,
    ).one()
    job.total_pages, job.total_count, job.seen_count = pages, count, count
    job.next_page = pages + 1
    session.add(job)
    session.flush()
