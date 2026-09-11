"""按可信上游配额域准入，连接和候选编号均不能拆分服务共享额度。"""

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from redis import Redis

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.context import ChannelKind, FrozenTikTokRoute
from app.integrations.tiktok.sdk import AccountAdmissionDeferred
from app.jobs.admission import AdmissionPolicy, admitted_scope

PROTOCOL_OPERATIONS = frozenset(
    f"protocol.{name}"
    for name in (
        "discover",
        "initialize",
        "initialized",
        "list_tools",
        "stream",
        "close",
        "cancel",
    )
)
ACCOUNT_DIRECTORY_OPERATIONS = frozenset(
    {
        "accounts.authorization_facts",
        "accounts.list_bcs",
        "accounts.list_bc_assets",
        "accounts.list_bc_members",
        "accounts.list_asset_members",
        "accounts.list_authorized_advertisers",
        "accounts.get_advertisers",
    }
)


def quota_scope(
    *, channel: ChannelKind, app_id: str | None, verified_service_scope: str | None
) -> str:
    if channel == "OFFICIAL_MCP":
        if verified_service_scope is None:
            return "official-mcp:shared-unverified"
        if (
            not isinstance(verified_service_scope, str)
            or not verified_service_scope.strip()
        ):
            raise DomainError("admission_policy_invalid", "调用额度配置无效")
        # 来源只能是部署配置或已核实的上游事实，不接受前端指定 connection/attempt 作额度域。
        return f"official-mcp:{verified_service_scope}"
    if channel != "OFFICIAL_API":
        raise DomainError("admission_policy_invalid", "调用额度配置无效")
    if not isinstance(app_id, str) or not app_id.strip():
        raise DomainError("tiktok_app_not_configured", "等待配置开发者应用")
    return app_id


@contextmanager
def admit_tiktok_call(
    redis_client: Redis,
    *,
    route: FrozenTikTokRoute,
    advertiser_id: str | None,
    operation: str,
    scope: str,
    policy: AdmissionPolicy,
) -> Iterator[None]:
    if advertiser_id is not None and (
        not isinstance(advertiser_id, str) or not advertiser_id.strip()
    ):
        raise DomainError("account_required", "该操作必须指定广告账户")
    if (
        advertiser_id is None
        and operation not in PROTOCOL_OPERATIONS | ACCOUNT_DIRECTORY_OPERATIONS
    ):
        raise DomainError("account_required", "该操作必须指定广告账户")
    with admitted_scope(
        redis_client,
        app_scope=scope,
        endpoint=operation,
        tenant_id=route.tenant_id,
        advertiser_id=advertiser_id if advertiser_id is not None else "",
        policy=policy,
        denied_error=AccountAdmissionDeferred,
    ):
        yield


@contextmanager
def admit_candidate_call(
    redis_client: Redis,
    *,
    tenant_id: UUID,
    attempt_id: UUID,
    scope: str,
    operation: str,
    policy: AdmissionPolicy,
) -> Iterator[None]:
    if operation not in PROTOCOL_OPERATIONS | ACCOUNT_DIRECTORY_OPERATIONS:
        raise DomainError(
            "mcp_candidate_operation_forbidden", "候选授权只允许协议与账户目录读取"
        )
    if not isinstance(tenant_id, UUID) or not isinstance(attempt_id, UUID):
        raise DomainError("mcp_candidate_operation_forbidden", "候选授权范围无效")
    # 尚未绑定 BC，不能构造虚假的 route；attempt 只做身份关联，不进入任何额度键。
    with admitted_scope(
        redis_client,
        app_scope=scope,
        endpoint=operation,
        tenant_id=tenant_id,
        advertiser_id="",
        policy=policy,
        denied_error=AccountAdmissionDeferred,
    ):
        yield
