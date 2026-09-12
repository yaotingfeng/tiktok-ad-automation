"""共享 MCP 授权的管理员目录入口；无 BC 绑定也可发现，绝不复制候选令牌。"""

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from redis import Redis
from sqlalchemy import Engine
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.credentials import decrypt_credentials
from app.core.errors import DomainError
from app.integrations.tiktok.admission import (
    ACCOUNT_DIRECTORY_OPERATIONS,
    PROTOCOL_OPERATIONS,
    quota_scope,
)
from app.integrations.tiktok.bounded_resources import bounded_redis, bounded_session
from app.integrations.tiktok.contracts.accounts import (
    CandidateReadContext,
    RuntimeReadContext,
)
from app.integrations.tiktok.mcp.accounts import McpAccountsGateway
from app.integrations.tiktok.mcp.authorization import material_authorization
from app.integrations.tiktok.mcp.protocol import (
    load_mcp_protocol,
    load_tool_contracts,
    verify_tool_schema,
)
from app.integrations.tiktok.mcp.transport import BoundMCPClient, open_bound_mcp_client
from app.integrations.tiktok.mcp_auth.bootstrap import (
    directory_fresh,
    observed_subject,
    read_business_centers,
    read_tools,
)
from app.integrations.tiktok.mcp_auth.refresh import ensure_mcp_credentials
from app.integrations.tiktok.mcp_auth.service import require_mcp_admin
from app.integrations.tiktok.sdk import AccountAdmissionDeferred
from app.jobs.admission import admission_policy, admitted_scope
from app.modules.accounts.connection_models import (
    BCConnectionBinding,
    ConnectionAuthorization,
    ConnectionToolObservation,
)
from app.modules.accounts.models import DiscoveryRun, TikTokConnection


def management_connection(
    session: Session,
    *,
    context: TenantContext,
    connection_id: UUID,
    authorization_revision: int | None = None,
    run_id: UUID | None = None,
    expected_contract_revision: str | None = None,
) -> TikTokConnection:
    require_mcp_admin(session, actor_id=context.actor_id, tenant_id=context.tenant_id)
    connection = session.get(TikTokConnection, connection_id, populate_existing=True)
    if (
        connection is None
        or connection.tenant_id != context.tenant_id
        or connection.kind != "OFFICIAL_MCP"
        or connection.status != "ACTIVE"
        or not connection.credential_ciphertext
    ):
        raise DomainError("connection_unavailable", "当前租户 MCP 授权不可用")
    if (
        authorization_revision is not None
        and connection.authorization_revision != authorization_revision
    ):
        raise DomainError("discovery_stale", "共享授权已改变，请重新同步")
    if run_id is not None:
        run = session.get(DiscoveryRun, run_id, populate_existing=True)
        if (
            run is None
            or (run.tenant_id, run.actor_id, run.connection_id)
            != (context.tenant_id, context.actor_id, connection_id)
            or run.status not in {"RUNNING", "ADMISSION_WAIT"}
            or run.authorization_revision != connection.authorization_revision
            or run.mcp_candidate_attempt_id is not None
            or not run.bc_id
            or run.work.get("bc_id") != run.bc_id
        ):
            raise DomainError("discovery_stale", "BC 同步任务已失效")
        binding = session.get(
            BCConnectionBinding,
            (context.tenant_id, run.bc_id, connection_id),
            populate_existing=True,
        )
        if (
            binding is None
            or binding.status == "DISABLED"
            or binding.authorization_revision != run.authorization_revision
            or binding.revision != run.binding_revision
        ):
            raise DomainError("discovery_stale", "BC 绑定已改变")
    # 仅协议重观测可固定旧版本；业务读取和已有任务仍必须匹配当前发布合同。
    expected = expected_contract_revision or load_mcp_protocol().schema_manifest_sha256
    if (expected_contract_revision is not None and run_id is not None) or (
        connection.adapter_contract_revision != expected
    ):
        raise DomainError("route_contract_changed", "当前 MCP 接口契约已更新")
    return connection


def current_observation(
    session: Session, *, context: TenantContext, connection: TikTokConnection
) -> ConnectionToolObservation | None:
    # 授权修订绑定目录证据；普通令牌轮换不改变这些授权事实。
    return session.exec(
        select(ConnectionToolObservation)
        .where(
            ConnectionToolObservation.tenant_id == context.tenant_id,
            ConnectionToolObservation.connection_id == connection.id,
            col(ConnectionToolObservation.candidate_attempt_id).is_(None),
            ConnectionToolObservation.pagination_complete == True,  # noqa: E712
            ConnectionToolObservation.expected_contract_revision
            == connection.adapter_contract_revision,
            col(ConnectionToolObservation.call_evidence)[
                "authorization_revision"
            ].as_integer()
            == connection.authorization_revision,
        )
        .order_by(col(ConnectionToolObservation.observed_at).desc())
    ).first()


def verified_schemas(observation: ConnectionToolObservation) -> dict[str, Any]:
    schemas = deepcopy(observation.tool_schemas)
    if (
        observation.schema_digest
        != hashlib.sha256(
            json.dumps(schemas, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    ):
        raise DomainError("mcp_contract_changed", "MCP 工具合同证据无效")
    for contract in load_tool_contracts():
        if contract.operation in ACCOUNT_DIRECTORY_OPERATIONS:
            verify_tool_schema(contract, schemas.get(contract.tool_name, {}))
    return schemas


@contextmanager
def _management_client(
    *,
    database_engine: Engine,
    redis_client: Redis,
    context: TenantContext,
    connection_id: UUID,
    task_deadline: datetime,
    authorization_revision: int | None = None,
    run_id: UUID | None = None,
    observe_tools_only: bool = False,
    expected_contract_revision: str | None = None,
) -> Iterator[tuple[BoundMCPClient, dict[str, str], int]]:
    if expected_contract_revision is not None and not observe_tools_only:
        raise DomainError(
            "mcp_management_operation_forbidden", "旧合同只能重检协议目录"
        )

    def validate(session: Session) -> TikTokConnection:
        return management_connection(
            session,
            context=context,
            connection_id=connection_id,
            authorization_revision=authorization_revision,
            run_id=run_id,
            expected_contract_revision=expected_contract_revision,
        )

    with bounded_session(database_engine, task_deadline=task_deadline) as session:
        initial = validate(session)
        authorization_revision = initial.authorization_revision
    ensure_mcp_credentials(
        database_engine=database_engine,
        redis_client=redis_client,
        context=context,
        connection_id=connection_id,
        task_deadline=task_deadline,
    )
    with bounded_session(database_engine, task_deadline=task_deadline) as session:
        connection = validate(session)
        material = decrypt_credentials(
            tenant_id=context.tenant_id,
            ciphertext=connection.credential_ciphertext or "",
        )
        credential_revision = connection.credential_revision
        observation = (
            None
            if observe_tools_only
            else current_observation(session, context=context, connection=connection)
        )
        if observation is None and not observe_tools_only:
            raise DomainError(
                "gateway_tool_observation_required", "请先完成当前授权的工具合同核验"
            )
        schemas = verified_schemas(observation) if observation is not None else {}
    facts = material_authorization(material, observed_at=datetime.now(UTC))
    if facts.evidence_source != "MCP_TOKEN_SCOPE_WITH_CLIENT":
        raise DomainError("gateway_authorization_invalid", "MCP 授权材料尚未核实")
    contracts = {
        c.operation: c
        for c in load_tool_contracts()
        if c.operation in ACCOUNT_DIRECTORY_OPERATIONS
    }

    def authorize(advertiser_id: str | None, operation: str) -> None:
        if (
            advertiser_id is not None
            or operation not in PROTOCOL_OPERATIONS | ACCOUNT_DIRECTORY_OPERATIONS
            or (observe_tools_only and operation not in PROTOCOL_OPERATIONS)
        ):
            raise DomainError(
                "mcp_management_operation_forbidden", "授权管理只允许账户目录读取"
            )
        with bounded_session(database_engine, task_deadline=task_deadline) as session:
            validate(session)
        ensure_mcp_credentials(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            connection_id=connection_id,
            task_deadline=task_deadline,
        )
        with bounded_session(database_engine, task_deadline=task_deadline) as session:
            connection = validate(session)
            if connection.credential_revision != credential_revision:
                raise DomainError(
                    "gateway_credentials_changed",
                    "访问凭据已更新，请重新打开调用会话",
                    retryable=True,
                )

    @contextmanager
    def admit(advertiser_id: str | None, operation: str) -> Iterator[None]:
        if (
            advertiser_id is not None
            or operation not in PROTOCOL_OPERATIONS | ACCOUNT_DIRECTORY_OPERATIONS
            or (observe_tools_only and operation not in PROTOCOL_OPERATIONS)
        ):
            raise DomainError(
                "mcp_management_operation_forbidden", "授权管理只允许账户目录读取"
            )
        policy = admission_policy(operation)
        remaining_ms = (task_deadline - datetime.now(UTC)).total_seconds() * 1000
        if remaining_ms <= 0 or policy.lease_ms <= remaining_ms + 1000:
            raise DomainError(
                "admission_policy_invalid", "调用租约必须覆盖管理读取时限"
            )
        scope = quota_scope(
            channel="OFFICIAL_MCP",
            app_id=None,
            verified_service_scope=settings.MCP_SERVICE_QUOTA_SCOPE,
        )
        with bounded_redis(redis_client, task_deadline=task_deadline) as bounded:
            # 与所有 BC/候选共享服务配额；不构造假候选或虚假执行路由。
            with admitted_scope(
                bounded,
                app_scope=scope,
                endpoint=operation,
                tenant_id=context.tenant_id,
                advertiser_id="",
                policy=policy,
                denied_error=AccountAdmissionDeferred,
            ):
                yield

    try:
        with open_bound_mcp_client(
            token=material["access_token"],
            task_deadline=task_deadline,
            authorize=authorize,
            admit=admit,
            contracts=contracts,
            observed_tools=schemas,
        ) as client:
            yield client, material, authorization_revision
    finally:
        material.clear()


@contextmanager
def open_management_accounts(
    *,
    database_engine: Engine,
    redis_client: Redis,
    context: TenantContext,
    connection_id: UUID,
    task_deadline: datetime,
    authorization_revision: int | None = None,
    run_id: UUID | None = None,
) -> Iterator[McpAccountsGateway]:
    with _management_client(
        database_engine=database_engine,
        redis_client=redis_client,
        context=context,
        connection_id=connection_id,
        task_deadline=task_deadline,
        authorization_revision=authorization_revision,
        run_id=run_id,
    ) as (client, material, _):
        facts = material_authorization(material, observed_at=datetime.now(UTC))
        # 管理列表可枚举可见 BC；具体同步任务则严格限制资产/角色读取到该 BC。
        read_context: CandidateReadContext | RuntimeReadContext = CandidateReadContext()
        if run_id is not None:
            with bounded_session(
                database_engine, task_deadline=task_deadline
            ) as session:
                run = session.get(DiscoveryRun, run_id)
                assert run is not None and run.bc_id is not None
                read_context = RuntimeReadContext(run.bc_id)
        yield McpAccountsGateway(client, context=read_context, authorization=facts)


def connection_business_centers(
    *,
    database_engine: Engine,
    redis_client: Redis,
    context: TenantContext,
    connection_id: UUID,
    task_deadline: datetime,
    refresh: bool = False,
) -> list[dict[str, Any]]:
    with bounded_session(database_engine, task_deadline=task_deadline) as session:
        current = session.get(TikTokConnection, connection_id)
        # 这里只读取版本供后续严格归属/权限校验，不以旧目录推定新接口可用。
        previous_contract = current.adapter_contract_revision if current else None
        connection = management_connection(
            session,
            context=context,
            connection_id=connection_id,
            expected_contract_revision=previous_contract,
        )
        revision = connection.authorization_revision
        needs_upgrade = previous_contract != load_mcp_protocol().schema_manifest_sha256
        existing = (
            None
            if needs_upgrade
            else current_observation(session, context=context, connection=connection)
        )
        if existing is not None:
            verified_schemas(existing)
        cached = existing is not None and directory_fresh(existing) and not refresh
        items = (
            deepcopy(existing.call_evidence["business_centers"])
            if cached and existing is not None
            else []
        )
    if not cached:
        if existing is None:
            # 已有授权升级或工具缓存缺失时，以当前凭据重新核实实际工具。
            # 此会话只能调用协议，不能拿预期合同代替真实观察发业务请求。
            with _management_client(
                database_engine=database_engine,
                redis_client=redis_client,
                context=context,
                connection_id=connection_id,
                task_deadline=task_deadline,
                authorization_revision=revision,
                observe_tools_only=True,
                expected_contract_revision=previous_contract,
            ) as (client, _, _):
                schemas, unavailable = read_tools(client)
            with bounded_session(
                database_engine, task_deadline=task_deadline
            ) as session:
                session.exec(
                    select(TikTokConnection)
                    .where(
                        TikTokConnection.id == connection_id,
                        TikTokConnection.tenant_id == context.tenant_id,
                    )
                    .with_for_update()
                ).one()
                checked_connection = management_connection(
                    session,
                    context=context,
                    connection_id=connection_id,
                    authorization_revision=revision,
                    expected_contract_revision=previous_contract,
                )
                # 完整真实目录校验后原子更新接口版本，保留授权、凭据、BC 绑定代数。
                # 旧冻结任务继续携带旧版本并自然阻断，不能随连接升级迁移历史意图。
                checked_connection.adapter_contract_revision = (
                    load_mcp_protocol().schema_manifest_sha256
                )
                session.add(checked_connection)
                session.add(
                    ConnectionToolObservation(
                        tenant_id=context.tenant_id,
                        connection_id=connection_id,
                        schema_digest=hashlib.sha256(
                            json.dumps(
                                schemas, sort_keys=True, separators=(",", ":")
                            ).encode()
                        ).hexdigest(),
                        expected_contract_revision=load_mcp_protocol().schema_manifest_sha256,
                        pagination_complete=True,
                        tool_schemas=schemas,
                        call_evidence={
                            "kind": "MANAGEMENT_SCHEMA_OBSERVATION",
                            "authorization_revision": revision,
                            "unavailable_tools": unavailable,
                        },
                    )
                )
                session.commit()
        with _management_client(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            connection_id=connection_id,
            task_deadline=task_deadline,
            authorization_revision=revision,
        ) as (client, material, _):
            schemas, unavailable = read_tools(client)
            gateway = McpAccountsGateway(
                client,
                context=CandidateReadContext(),
                authorization=material_authorization(
                    material, observed_at=datetime.now(UTC)
                ),
            )
            facts = observed_subject(gateway)
            items = read_business_centers(gateway)
        with bounded_session(database_engine, task_deadline=task_deadline) as session:
            session.exec(
                select(TikTokConnection)
                .where(TikTokConnection.id == connection_id)
                .with_for_update()
            ).one()
            management_connection(
                session,
                context=context,
                connection_id=connection_id,
                authorization_revision=revision,
            )
            auth = session.exec(
                select(ConnectionAuthorization).where(
                    ConnectionAuthorization.tenant_id == context.tenant_id,
                    ConnectionAuthorization.connection_id == connection_id,
                    ConnectionAuthorization.authorization_revision == revision,
                )
            ).one()
            if facts.get("subject_id") != auth.upstream_subject or set(
                facts.get("scopes", [])
            ) != set(auth.scopes):
                # 观察到授权边界变化即关闭整份共享授权；不能只提示错误后继续使用旧权限。
                changed = session.get(TikTokConnection, connection_id)
                assert changed is not None
                changed.status = "REAUTH_REQUIRED"
                changed.authorization_revision += 1
                session.add(changed)
                session.commit()
                raise DomainError(
                    "mcp_authorization_mismatch",
                    "当前 MCP 授权主体或范围已改变，请重新授权",
                )
            session.add(
                ConnectionToolObservation(
                    tenant_id=context.tenant_id,
                    connection_id=connection_id,
                    schema_digest=hashlib.sha256(
                        json.dumps(
                            schemas, sort_keys=True, separators=(",", ":")
                        ).encode()
                    ).hexdigest(),
                    expected_contract_revision=load_mcp_protocol().schema_manifest_sha256,
                    pagination_complete=True,
                    tool_schemas=schemas,
                    call_evidence={
                        "kind": "MANAGEMENT_DIRECTORY",
                        "authorization_revision": revision,
                        "business_centers_complete": True,
                        "business_centers": items,
                        "business_centers_checked_at": datetime.now(UTC).isoformat(),
                        "authorization_facts": facts,
                        "unavailable_tools": unavailable,
                    },
                )
            )
            session.commit()
    with bounded_session(database_engine, task_deadline=task_deadline) as session:
        management_connection(
            session,
            context=context,
            connection_id=connection_id,
            authorization_revision=revision,
        )
        bindings = {
            row.bc_id: row
            for row in session.exec(
                select(BCConnectionBinding).where(
                    BCConnectionBinding.tenant_id == context.tenant_id,
                    BCConnectionBinding.connection_id == connection_id,
                )
            ).all()
        }
        return [
            {
                **item,
                "connected": item["bc_id"] in bindings
                and bindings[item["bc_id"]].status != "DISABLED",
                "binding_status": bindings[item["bc_id"]].status
                if item["bc_id"] in bindings
                else None,
            }
            for item in items
        ]
