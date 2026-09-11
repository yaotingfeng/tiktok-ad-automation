"""任务独占双通道会话；业务代码只取得固定 BC 的 typed gateway。"""

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from redis import Redis
from sqlalchemy import Engine, or_
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.credentials import decrypt_credentials
from app.core.errors import DomainError
from app.integrations.tiktok.adapters.mcp_builds import McpBuildOperations
from app.integrations.tiktok.adapters.mcp_materials import MCPMaterialOperations
from app.integrations.tiktok.adapters.sdk_builds import ApiBuildOperations
from app.integrations.tiktok.adapters.sdk_materials import SDKMaterialOperations
from app.integrations.tiktok.admission import (
    PROTOCOL_OPERATIONS,
    admit_tiktok_call,
    quota_scope,
)
from app.integrations.tiktok.bounded_resources import bounded_redis, bounded_session
from app.integrations.tiktok.contracts.accounts import (
    AccountsGateway,
    AuthorizationFacts,
    RuntimeReadContext,
)
from app.integrations.tiktok.contracts.builds import BuildOperations
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.integrations.tiktok.contracts.materials import MaterialOperations
from app.integrations.tiktok.contracts.scenes import ScenesGateway
from app.integrations.tiktok.material_upload_evidence import material_upload_policy
from app.integrations.tiktok.mcp.accounts import McpAccountsGateway
from app.integrations.tiktok.mcp.authorization import (
    material_authorization as mcp_material_authorization,
)
from app.integrations.tiktok.mcp.protocol import load_mcp_protocol, load_tool_contracts
from app.integrations.tiktok.mcp.scenes import McpScenesGateway
from app.integrations.tiktok.mcp.transport import open_bound_mcp_client
from app.integrations.tiktok.mcp_auth.refresh import ensure_mcp_credentials
from app.integrations.tiktok.official.accounts import OfficialAccountsGateway
from app.integrations.tiktok.official.authorization import (
    material_authorization as api_material_authorization,
)
from app.integrations.tiktok.official.scenes import OfficialScenesGateway
from app.integrations.tiktok.sdk import official_client
from app.jobs.admission import admission_policy
from app.modules.accounts.connection_models import (
    ConnectionAuthorization,
    ConnectionToolObservation,
)
from app.modules.accounts.models import TikTokConnection
from app.modules.accounts.routing import Capability, verify_route

# 按精确操作登记所需权限；不得按前缀把未来写操作默认为 read。
_OPERATION_CAPABILITIES: dict[str, Capability] = {
    **dict.fromkeys(PROTOCOL_OPERATIONS, "read"),
    "accounts.authorization_facts": "read",
    "accounts.list_bcs": "read",
    "accounts.list_bc_assets": "read",
    "accounts.list_bc_members": "read",
    "accounts.list_asset_members": "read",
    "accounts.list_authorized_advertisers": "read",
    "accounts.get_advertisers": "read",
    "scene.list_identities": "read",
    "scene.list_minis": "read",
    "scene.recommend_ctas": "read",
    "scene.list_regions": "read",
    "scene.check_vbo": "read",
    "materials.get_videos": "read",
    "materials.search_videos": "read",
    "materials.get_suggested_covers": "read",
    "materials.get_images": "read",
    "materials.search_images": "read",
    "materials.upload_video_url": "upload",
    "materials.upload_video_file": "upload",
    "build.get_campaigns": "read",
    "build.get_adgroups": "read",
    "build.get_ads": "read",
    "build.get_cta_portfolio": "read",
    "build.get_regular_adgroups": "read",
    "build.create_campaign": "build",
    "build.create_adgroup": "build",
    "build.create_ad": "build",
    "build.create_cta_portfolio": "build",
}
_DIRECTORY_OPERATIONS = frozenset(
    operation
    for operation in _OPERATION_CAPABILITIES
    if operation.startswith("accounts.")
)


@dataclass(frozen=True, repr=False)
class TikTokGateway:
    accounts: AccountsGateway
    scenes: ScenesGateway
    materials: MaterialOperations
    builds: BuildOperations


def _capability(advertiser_id: str | None, operation: str) -> Capability:
    capability = _OPERATION_CAPABILITIES.get(operation)
    if capability is None:
        raise DomainError("gateway_operation_forbidden", "本次会话不支持该操作")
    if (
        advertiser_id is None
        and operation not in PROTOCOL_OPERATIONS | _DIRECTORY_OPERATIONS
    ):
        raise DomainError("account_required", "该操作必须指定广告账户")
    if (
        advertiser_id is not None
        and operation in PROTOCOL_OPERATIONS | _DIRECTORY_OPERATIONS
    ):
        raise DomainError("gateway_operation_forbidden", "目录操作归属参数无效")
    return capability


def _authorization(
    session: Session, route: FrozenTikTokRoute
) -> ConnectionAuthorization | None:
    return session.exec(
        select(ConnectionAuthorization).where(
            ConnectionAuthorization.tenant_id == route.tenant_id,
            ConnectionAuthorization.connection_id == route.connection_id,
            ConnectionAuthorization.authorization_revision
            == route.authorization_revision,
        )
    ).one_or_none()


def _facts(
    authorization: ConnectionAuthorization | None, route: FrozenTikTokRoute
) -> AuthorizationFacts:
    # UNKNOWN 的坐标只是固定服务地址，不是已验证 OAuth 声明；epoch 保证其不构成新鲜授权。
    if route.channel == "OFFICIAL_MCP":
        profile = load_mcp_protocol()
        issuer, resource = profile.issuer, profile.resource
    else:
        issuer = "https://business-api.tiktok.com"
        resource = "https://business-api.tiktok.com/open_api/v1.3"
    assert issuer is not None and resource is not None
    try:
        return AuthorizationFacts(
            subject_id=authorization.upstream_subject if authorization else None,
            grant_id=authorization.upstream_grant_id if authorization else None,
            issuer=authorization.issuer or issuer if authorization else issuer,
            resource=authorization.resource or resource if authorization else resource,
            scopes=tuple(authorization.scopes) if authorization else (),
            read_authorized=authorization.permission_summary.get("read_authorized")
            if authorization
            else None,
            upload_authorized=authorization.permission_summary.get("upload_authorized")
            if authorization
            else None,
            build_authorized=authorization.permission_summary.get("build_authorized")
            if authorization
            else None,
            evidence_source=authorization.source if authorization else "UNKNOWN",
            observed_at=authorization.verified_at or datetime.fromtimestamp(0, UTC)
            if authorization
            else datetime.fromtimestamp(0, UTC),
        )
    except ValueError, TypeError:
        raise DomainError("gateway_authorization_invalid", "当前授权摘要无效") from None


@contextmanager
def open_tiktok_gateway(
    *,
    database_engine: Engine,
    redis_client: Redis,
    context: TenantContext,
    route: FrozenTikTokRoute,
    task_deadline: datetime,
    before_request: Callable[[], None] | None = None,
) -> Iterator[TikTokGateway]:
    # 打开前与每个物理发送前都检查原 route；整个工厂从不重新读取 BC 默认。
    with bounded_session(database_engine, task_deadline=task_deadline) as session:
        verify_route(
            session, context=context, route=route, advertiser_id=None, capability="read"
        )
    if route.channel == "OFFICIAL_MCP":
        ensure_mcp_credentials(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            connection_id=route.connection_id,
            task_deadline=task_deadline,
        )
    else:
        settings.require_tiktok_app()
    with bounded_session(database_engine, task_deadline=task_deadline) as session:
        verify_route(
            session, context=context, route=route, advertiser_id=None, capability="read"
        )
        connection = session.get(TikTokConnection, route.connection_id)
        assert connection is not None
        material = decrypt_credentials(
            tenant_id=context.tenant_id,
            ciphertext=connection.credential_ciphertext or "",
        )
        token = material.get("access_token")
        if not token or any(character in token for character in ("\r", "\n")):
            raise DomainError("credential_invalid", "凭据缺少访问令牌")
        credential_revision = connection.credential_revision
        authorization = _authorization(session, route)
        facts = _facts(authorization, route)
        # 重新观测只携带当前凭据的实际授权声明，不能替换尚未完整核实的持久事实。
        observation_facts = (
            mcp_material_authorization
            if route.channel == "OFFICIAL_MCP"
            else api_material_authorization
        )(material, observed_at=datetime.now(UTC))
        observed: dict[str, Any] = {}
        if route.channel == "OFFICIAL_MCP":
            profile = load_mcp_protocol()
            if route.adapter_contract_revision != profile.schema_manifest_sha256:
                raise DomainError("route_contract_changed", "当前适配契约已更新")
            if (material.get("issuer"), material.get("resource")) != (
                profile.issuer,
                profile.resource,
            ):
                raise DomainError("gateway_authorization_invalid", "MCP 授权服务不匹配")
            current_candidate = (
                authorization.mcp_authorization_attempt_id if authorization else None
            )
            observation = session.exec(
                select(ConnectionToolObservation)
                .where(
                    ConnectionToolObservation.tenant_id == route.tenant_id,
                    ConnectionToolObservation.connection_id == route.connection_id,
                    ConnectionToolObservation.expected_contract_revision
                    == route.adapter_contract_revision,
                    ConnectionToolObservation.pagination_complete == True,  # noqa: E712
                    or_(
                        col(ConnectionToolObservation.candidate_attempt_id).is_(None),
                        col(ConnectionToolObservation.candidate_attempt_id)
                        == current_candidate,
                    ),
                )
                .order_by(col(ConnectionToolObservation.observed_at).desc())
            ).first()
            if observation is None:
                raise DomainError(
                    "gateway_tool_observation_required",
                    "请先完成当前连接的工具合同核验",
                )
            observed = json.loads(json.dumps(observation.tool_schemas))

    # 此后不保留 Session；portal 回调各自拥有独立、限时的数据库事务。
    def authorize(advertiser_id: str | None, operation: str) -> None:
        if before_request is not None:
            before_request()
        capability = _capability(advertiser_id, operation)
        with bounded_session(database_engine, task_deadline=task_deadline) as session:
            verify_route(
                session,
                context=context,
                route=route,
                advertiser_id=advertiser_id,
                capability=capability,
            )
        if route.channel == "OFFICIAL_MCP":
            ensure_mcp_credentials(
                database_engine=database_engine,
                redis_client=redis_client,
                context=context,
                connection_id=route.connection_id,
                task_deadline=task_deadline,
            )
        with bounded_session(database_engine, task_deadline=task_deadline) as session:
            verify_route(
                session,
                context=context,
                route=route,
                advertiser_id=advertiser_id,
                capability=capability,
            )
            row = session.get(TikTokConnection, route.connection_id)
            if row is None or row.credential_revision != credential_revision:
                # 当前会话内存 header 已冻结；发送前退出后可按同一 route 新建会话，不重放已发调用。
                raise DomainError(
                    "gateway_credentials_changed",
                    "访问凭据已更新，请重新打开调用会话",
                    retryable=True,
                )

    @contextmanager
    def admit(advertiser_id: str | None, operation: str) -> Iterator[None]:
        _capability(advertiser_id, operation)
        policy = admission_policy(operation)
        remaining_ms = (task_deadline - datetime.now(UTC)).total_seconds() * 1000
        if remaining_ms <= 0 or policy.lease_ms <= remaining_ms + 1000:
            raise DomainError(
                "admission_policy_invalid", "调用租约必须覆盖本次任务期限"
            )
        scope = quota_scope(
            channel=route.channel,
            app_id=settings.TIKTOK_APP_ID if route.channel == "OFFICIAL_API" else None,
            verified_service_scope=settings.MCP_SERVICE_QUOTA_SCOPE,
        )
        with bounded_redis(redis_client, task_deadline=task_deadline) as bounded:
            with admit_tiktok_call(
                bounded,
                route=route,
                advertiser_id=advertiser_id,
                operation=operation,
                scope=scope,
                policy=policy,
            ):
                yield

    @contextmanager
    def request_scope(
        advertiser_id: str | None, operation: str, deadline: datetime
    ) -> Iterator[None]:
        if deadline != task_deadline:
            raise DomainError("read_deadline_invalid", "调用期限与任务不一致")
        authorize(advertiser_id, operation)
        with admit(advertiser_id, operation):
            authorize(advertiser_id, operation)
            yield

    read_context = RuntimeReadContext(route.bc_id)
    try:
        if route.channel == "OFFICIAL_MCP":
            contracts = {
                contract.operation: contract
                for contract in load_tool_contracts()
                if contract.operation in _OPERATION_CAPABILITIES
            }
            with open_bound_mcp_client(
                token=token,
                task_deadline=task_deadline,
                authorize=authorize,
                admit=admit,
                contracts=contracts,
                observed_tools=observed,
            ) as client:
                yield TikTokGateway(
                    accounts=McpAccountsGateway(
                        client,
                        context=read_context,
                        authorization=facts,
                        observation_authorization=observation_facts,
                    ),
                    scenes=McpScenesGateway(client, context=read_context),
                    builds=McpBuildOperations(client),
                    materials=MCPMaterialOperations(
                        client,
                        preview_allowed_hosts=settings.MATERIAL_REMOTE_MEDIA_HOSTS,
                        upload_policy=material_upload_policy(
                            channel=route.channel,
                            adapter_contract_revision=route.adapter_contract_revision,
                        ),
                    ),
                )
        else:
            with official_client(access_token=token) as official:
                yield TikTokGateway(
                    accounts=OfficialAccountsGateway(
                        official,
                        context=read_context,
                        authorization=facts,
                        observation_authorization=observation_facts,
                        app_id=settings.TIKTOK_APP_ID,
                        secret=settings.TIKTOK_APP_SECRET,
                        request_scope=request_scope,
                        deadline=task_deadline,
                    ),
                    builds=ApiBuildOperations(
                        official, request_scope=request_scope, deadline=task_deadline
                    ),
                    materials=SDKMaterialOperations(
                        official,
                        request_scope=request_scope,
                        deadline=task_deadline,
                        preview_allowed_hosts=settings.MATERIAL_REMOTE_MEDIA_HOSTS,
                    ),
                    scenes=OfficialScenesGateway(
                        official,
                        context=read_context,
                        request_scope=request_scope,
                        deadline=task_deadline,
                    ),
                )
    finally:
        material.clear()
        token = None
