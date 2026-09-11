"""候选只读入口：每次 HTTP 重新核对管理员，先完整观测合同再读取目录。"""

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
from app.integrations.tiktok.admission import admit_candidate_call, quota_scope
from app.integrations.tiktok.bounded_resources import bounded_redis, bounded_session
from app.integrations.tiktok.contracts.accounts import (
    AccountsGateway,
    AuthorizationFacts,
    CandidateReadContext,
)
from app.integrations.tiktok.mcp.protocol import (
    _semantic_schema,
    load_mcp_protocol,
    load_tool_contracts,
    verify_tool_schema,
)
from app.integrations.tiktok.mcp.transport import BoundMCPClient, open_bound_mcp_client
from app.integrations.tiktok.mcp_auth.service import require_mcp_admin
from app.jobs.admission import admission_policy
from app.modules.accounts.connection_models import (
    ConnectionToolObservation,
    McpAuthorizationAttempt,
)
from app.modules.accounts.models import TikTokConnection


def candidate_attempt(
    session: Session, *, context: TenantContext, attempt_id: UUID
) -> McpAuthorizationAttempt:
    require_mcp_admin(session, actor_id=context.actor_id, tenant_id=context.tenant_id)
    attempt = session.get(McpAuthorizationAttempt, attempt_id, populate_existing=True)
    if attempt is None or attempt.tenant_id != context.tenant_id:
        raise DomainError("mcp_candidate_unavailable", "候选授权不可用")
    connection = session.get(
        TikTokConnection, attempt.connection_id, populate_existing=True
    )
    if (
        connection is None
        or connection.tenant_id != context.tenant_id
        or connection.kind != "OFFICIAL_MCP"
        or connection.status == "DISABLED"
        or attempt.status != "CANDIDATE_READY"
        or not attempt.candidate_ciphertext
        or attempt.expires_at <= datetime.now(UTC)
        or (attempt.base_credential_revision, attempt.base_authorization_revision)
        != (connection.credential_revision, connection.authorization_revision)
    ):
        raise DomainError("mcp_candidate_unavailable", "候选授权不可用")
    return attempt


def _observation(
    session: Session, *, context: TenantContext, attempt_id: UUID
) -> ConnectionToolObservation | None:
    return session.exec(
        select(ConnectionToolObservation)
        .where(
            ConnectionToolObservation.tenant_id == context.tenant_id,
            ConnectionToolObservation.candidate_attempt_id == attempt_id,
            ConnectionToolObservation.pagination_complete == True,  # noqa: E712
        )
        .order_by(col(ConnectionToolObservation.observed_at).desc())
    ).first()


def _material(
    session: Session, *, context: TenantContext, attempt_id: UUID
) -> tuple[McpAuthorizationAttempt, dict[str, str]]:
    attempt = candidate_attempt(session, context=context, attempt_id=attempt_id)
    material = decrypt_credentials(
        tenant_id=context.tenant_id, ciphertext=attempt.candidate_ciphertext or ""
    )
    profile = load_mcp_protocol()
    if (
        material.get("issuer") != profile.issuer
        or material.get("resource") != profile.resource
    ):
        raise DomainError("mcp_candidate_unavailable", "候选授权不可用")
    if "expires_at" in material:
        try:
            expired = datetime.fromisoformat(material["expires_at"]) <= datetime.now(
                UTC
            )
        except ValueError, TypeError:
            expired = True
        if expired:
            raise DomainError("mcp_candidate_unavailable", "候选授权已过期")
    return attempt, material


@contextmanager
def _candidate_client(
    *,
    database_engine: Engine,
    redis_client: Redis,
    context: TenantContext,
    attempt_id: UUID,
    task_deadline: datetime,
    observed_tools: dict[str, Any],
) -> Iterator[BoundMCPClient]:
    with bounded_session(database_engine, task_deadline=task_deadline) as session:
        _, material = _material(session, context=context, attempt_id=attempt_id)
    contracts = {
        item.operation: item
        for item in load_tool_contracts()
        if item.operation.startswith("accounts.")
    }

    def authorize(advertiser_id: str | None, operation: str) -> None:
        if advertiser_id is not None or not (
            operation.startswith("protocol.") or operation in contracts
        ):
            raise DomainError(
                "mcp_candidate_operation_forbidden", "候选授权只允许账户目录读取"
            )
        # Portal HTTP 线程各自打开短 Session；绝不捕获 FastAPI 线程的 Session。
        with bounded_session(database_engine, task_deadline=task_deadline) as session:
            _material(session, context=context, attempt_id=attempt_id)

    @contextmanager
    def admit(_advertiser_id: str | None, operation: str) -> Iterator[None]:
        policy = admission_policy(operation)
        remaining_ms = (task_deadline - datetime.now(UTC)).total_seconds() * 1000
        if remaining_ms <= 0 or policy.lease_ms <= remaining_ms + 1000:
            raise DomainError(
                "admission_policy_invalid", "调用租约必须覆盖候选读取时限"
            )
        scope = quota_scope(
            channel="OFFICIAL_MCP",
            app_id=None,
            verified_service_scope=settings.MCP_SERVICE_QUOTA_SCOPE,
        )
        with bounded_redis(redis_client, task_deadline=task_deadline) as bounded:
            with admit_candidate_call(
                bounded,
                tenant_id=context.tenant_id,
                attempt_id=attempt_id,
                scope=scope,
                operation=operation,
                policy=policy,
            ):
                yield

    with open_bound_mcp_client(
        token=material["access_token"],
        task_deadline=task_deadline,
        authorize=authorize,
        admit=admit,
        contracts=contracts,
        observed_tools=observed_tools,
    ) as client:
        yield client


def observe_candidate_tools(
    *,
    database_engine: Engine,
    redis_client: Redis,
    context: TenantContext,
    attempt_id: UUID,
    task_deadline: datetime,
) -> UUID:
    with bounded_session(database_engine, task_deadline=task_deadline) as session:
        attempt = candidate_attempt(session, context=context, attempt_id=attempt_id)
        connection_id = attempt.connection_id
        existing = _observation(session, context=context, attempt_id=attempt_id)
        if existing is not None:
            return existing.id
    required = {
        c.tool_name: c
        for c in load_tool_contracts()
        if c.operation.startswith("accounts.")
    }
    reviewed = {c.tool_name: c for c in load_tool_contracts()}
    schemas = {}
    unavailable_tools = {}
    seen = set()
    with _candidate_client(
        database_engine=database_engine,
        redis_client=redis_client,
        context=context,
        attempt_id=attempt_id,
        task_deadline=task_deadline,
        observed_tools={},
    ) as client:
        cursor = None
        for _ in range(100):
            page = client.list_tools(cursor=cursor)
            for tool in page.tools:
                if tool.name in seen:
                    raise DomainError("mcp_contract_changed", "MCP 目录包含重复工具")
                seen.add(tool.name)
                if len(seen) > 10000:
                    raise DomainError("mcp_contract_changed", "MCP 目录超过核验上限")
                contract = reviewed.get(tool.name)
                if contract is not None:
                    observed = tool.model_dump(by_alias=True, exclude_none=True)
                    try:
                        verify_tool_schema(contract, observed)
                    except DomainError as error:
                        if (
                            tool.name in required
                            or error.code != "mcp_contract_changed"
                        ):
                            raise
                        # 可选写入/素材能力的漂移只使该工具不可用，不能阻断完整账户读取。
                        unavailable_tools[tool.name] = "mcp_contract_changed"
                        continue
                    # 从实际观察中保留经核实的 schema 语义，不拿 expected manifest 冒充观察。
                    # 删除 schema 注解和 SDK 扩展，避免保存描述或任意签名 URL。
                    schemas[tool.name] = {
                        "name": tool.name,
                        "inputSchema": _semantic_schema(observed["inputSchema"]),
                    }
                    if contract.output_schema is not None:
                        schemas[tool.name]["outputSchema"] = _semantic_schema(
                            observed["outputSchema"]
                        )
            cursor = page.next_cursor
            if cursor is None:
                break
        else:
            raise DomainError("mcp_contract_changed", "MCP 目录分页未完成")
    if not set(required) <= set(schemas):
        raise DomainError("mcp_contract_changed", "MCP 账户读取合同不完整")
    profile = load_mcp_protocol()
    with bounded_session(database_engine, task_deadline=task_deadline) as session:
        candidate_attempt(session, context=context, attempt_id=attempt_id)
        observation = ConnectionToolObservation(
            tenant_id=context.tenant_id,
            connection_id=connection_id,
            candidate_attempt_id=attempt_id,
            schema_digest=hashlib.sha256(
                json.dumps(schemas, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            expected_contract_revision=profile.schema_manifest_sha256,
            pagination_complete=True,
            tool_schemas=schemas,
            call_evidence={
                "kind": "CANDIDATE_SCHEMA_OBSERVATION",
                "unavailable_tools": unavailable_tools,
            },
        )
        session.add(observation)
        session.commit()
        return observation.id


@contextmanager
def open_candidate_accounts(
    *,
    database_engine: Engine,
    redis_client: Redis,
    context: TenantContext,
    attempt_id: UUID,
    task_deadline: datetime,
) -> Iterator[AccountsGateway]:
    observation_id = observe_candidate_tools(
        database_engine=database_engine,
        redis_client=redis_client,
        context=context,
        attempt_id=attempt_id,
        task_deadline=task_deadline,
    )
    with bounded_session(database_engine, task_deadline=task_deadline) as session:
        _, material = _material(session, context=context, attempt_id=attempt_id)
        observation = session.get(ConnectionToolObservation, observation_id)
        if observation is None:
            raise DomainError("mcp_candidate_unavailable", "候选合同观测不可用")
        schemas = deepcopy(observation.tool_schemas)
    facts = AuthorizationFacts(
        subject_id=None,
        grant_id=None,
        issuer=material["issuer"],
        resource=material["resource"],
        scopes=tuple(json.loads(material["scopes"])),
        read_authorized=None,
        upload_authorized=None,
        build_authorized=None,
        evidence_source="OAUTH_TOKEN_RESPONSE",
        observed_at=datetime.fromisoformat(material["received_at"]),
    )
    from app.integrations.tiktok.mcp.accounts import McpAccountsGateway

    class CandidateAccountsGateway(McpAccountsGateway):
        def authorization_facts(self) -> AuthorizationFacts:
            # 此方法只返回内存事实，不会经过 HTTP 回调，仍须检查当前候选管理员。
            with bounded_session(
                database_engine, task_deadline=task_deadline
            ) as session:
                candidate_attempt(session, context=context, attempt_id=attempt_id)
            return super().authorization_facts()

    with _candidate_client(
        database_engine=database_engine,
        redis_client=redis_client,
        context=context,
        attempt_id=attempt_id,
        task_deadline=task_deadline,
        observed_tools=schemas,
    ) as client:
        yield CandidateAccountsGateway(
            client, context=CandidateReadContext(), authorization=facts
        )


def candidate_business_centers(
    *,
    database_engine: Engine,
    redis_client: Redis,
    context: TenantContext,
    attempt_id: UUID,
    task_deadline: datetime,
) -> list[dict[str, str]]:
    items: dict[str, dict[str, str]] = {}
    totals = None
    with open_candidate_accounts(
        database_engine=database_engine,
        redis_client=redis_client,
        context=context,
        attempt_id=attempt_id,
        task_deadline=task_deadline,
    ) as gateway:
        for page_no in range(1, 101):
            page = gateway.business_centers(page=page_no, page_size=100)
            current_totals = (page.total_pages, page.total_number)
            if totals is not None and totals != current_totals:
                raise DomainError(
                    "mcp_candidate_directory_incomplete", "候选 BC 目录在分页期间变化"
                )
            totals = current_totals
            for bc in page.items:
                if bc.bc_id in items or len(bc.bc_id) > 128:
                    raise DomainError(
                        "mcp_candidate_directory_incomplete", "候选 BC 目录重复或无效"
                    )
                items[bc.bc_id] = {"bc_id": bc.bc_id, "name": bc.name}
            if page.last:
                break
        else:
            raise DomainError(
                "mcp_candidate_directory_incomplete", "候选 BC 目录分页未完成"
            )
    if totals is not None and totals[1] is not None and len(items) != totals[1]:
        raise DomainError(
            "mcp_candidate_directory_incomplete", "候选 BC 目录总数不一致"
        )
    with bounded_session(database_engine, task_deadline=task_deadline) as session:
        candidate_attempt(session, context=context, attempt_id=attempt_id)
        observation = _observation(session, context=context, attempt_id=attempt_id)
        if observation is None:
            raise DomainError("mcp_candidate_unavailable", "候选合同观测不可用")
        observation.call_evidence = {
            **observation.call_evidence,
            "business_centers_complete": True,
            "business_centers": list(items.values()),
            "business_centers_checked_at": datetime.now(UTC).isoformat(),
        }
        session.add(observation)
        session.commit()
    return list(items.values())
