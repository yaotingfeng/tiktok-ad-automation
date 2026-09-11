"""Redis-wide SDK request admission; never use a connection ID as app_scope.

Every actual SDK request needs a fresh lease_id. Denial is a rescheduling signal,
not a reason to sleep in a worker. Release in the SDK caller's finally block;
log release failures separately so they cannot overwrite a successful result or
turn it into a blindly retryable request. Configure leases longer than the SDK's
connect/read timeouts plus processing margin.
"""

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

from billiard.exceptions import SoftTimeLimitExceeded  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from redis.exceptions import RedisError

from app.core.config import settings
from app.core.errors import DomainError

_ADMISSION_SCRIPT = Path(__file__).with_suffix(".lua").read_text()
_RELEASE_SCRIPT = "for i=1,#KEYS do redis.call('ZREM',KEYS[i],ARGV[1]) end return 1"
_ENDPOINT_OVERRIDES = frozenset(
    {"endpoint_max_inflight", "endpoint_calls_per_window", "lease_ms"}
)


@dataclass(frozen=True)
class Admission:
    granted: bool
    retry_after_ms: int


class AdmissionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True)

    app_max_inflight: int = Field(gt=0)
    endpoint_max_inflight: int = Field(gt=0)
    tenant_max_inflight: int = Field(gt=0)
    advertiser_max_inflight: int = Field(gt=0)
    app_calls_per_window: int = Field(gt=0)
    endpoint_calls_per_window: int = Field(gt=0)
    window_ms: int = Field(gt=0)
    lease_ms: int = Field(gt=0)


def admission_policy(endpoint: str) -> AdmissionPolicy:
    config = settings.TIKTOK_CALL_POLICIES
    if config == {}:
        raise DomainError("admission_unconfigured", "请配置应用调用额度")
    try:
        if not isinstance(config, dict) or set(config) - {"base", "endpoints"}:
            raise ValueError("Invalid policy structure")
        base = config.get("base")
        endpoints = config.get("endpoints", {})
        if not isinstance(base, dict) or not isinstance(endpoints, dict):
            raise ValueError("Invalid policy structure")
        # Check every override: a malformed unused endpoint is still a broken
        # deployment configuration, never a reason to silently use base limits.
        for name, override in endpoints.items():
            if (
                not isinstance(name, str)
                or not isinstance(override, dict)
                or set(override) - _ENDPOINT_OVERRIDES
            ):
                raise ValueError("Endpoint cannot override shared quotas")
            AdmissionPolicy.model_validate({**base, **override})
        AdmissionPolicy.model_validate(base)
        return AdmissionPolicy.model_validate({**base, **endpoints.get(endpoint, {})})
    except (ValidationError, TypeError, ValueError) as error:
        raise DomainError("admission_policy_invalid", "调用额度配置无效") from error


def admission_keys(
    app_scope: str, endpoint: str, tenant_id: UUID, advertiser_id: str
) -> list[str]:
    """All six keys use the application hash tag for atomic Redis Cluster eval."""
    digest = sha256(app_scope.encode()).hexdigest()
    base = f"tiktok:{{{digest}}}"
    endpoint_key = sha256(endpoint.encode()).hexdigest()
    account_key = sha256(advertiser_id.encode()).hexdigest()
    return [
        f"{base}:rate",
        f"{base}:endpoint:{endpoint_key}:rate",
        f"{base}:active",
        f"{base}:endpoint:{endpoint_key}:active",
        f"{base}:tenant:{tenant_id}:active",
        f"{base}:advertiser:{account_key}:active",
    ]


def admit_call(
    redis_client,
    *,
    app_scope: str,
    endpoint: str,
    tenant_id: UUID,
    advertiser_id: str,
    lease_id: UUID,
    policy: AdmissionPolicy,
) -> Admission:
    keys = admission_keys(app_scope, endpoint, tenant_id, advertiser_id)
    args = [
        str(lease_id),
        policy.window_ms,
        policy.lease_ms,
        policy.app_calls_per_window,
        policy.endpoint_calls_per_window,
        policy.app_max_inflight,
        policy.endpoint_max_inflight,
        policy.tenant_max_inflight,
        policy.advertiser_max_inflight,
    ]
    try:
        granted, delay = redis_client.eval(_ADMISSION_SCRIPT, 6, *keys, *args)
    except RedisError as error:
        raise DomainError(
            "admission_unavailable", "调用配额服务暂不可用", True
        ) from error
    return Admission(bool(granted), int(delay))


def release_call(
    redis_client,
    *,
    app_scope: str,
    endpoint: str,
    tenant_id: UUID,
    advertiser_id: str,
    lease_id: UUID,
) -> None:
    """Release inflight leases only. RedisError is handled separately by caller."""
    keys = admission_keys(app_scope, endpoint, tenant_id, advertiser_id)[2:]
    redis_client.eval(_RELEASE_SCRIPT, len(keys), *keys, str(lease_id))


@contextmanager
def admitted_scope(
    redis_client,
    *,
    app_scope: str,
    endpoint: str,
    tenant_id: UUID,
    advertiser_id: str,
    policy: AdmissionPolicy,
    denied_error: Callable[[int], Exception],
    admit: Callable[..., Admission] = admit_call,
    release: Callable[..., None] = release_call,
) -> Iterator[None]:
    """两个通道共用租约生命周期；认证和上游配额域由调用边界提供。"""
    lease_id = uuid4()
    scope = {
        "app_scope": app_scope,
        "endpoint": endpoint,
        "tenant_id": tenant_id,
        "advertiser_id": advertiser_id,
        "lease_id": lease_id,
    }
    admission = admit(redis_client, **scope, policy=policy)
    if not admission.granted:
        raise denied_error(admission.retry_after_ms)
    interrupted = False
    try:
        yield
    except SystemExit, KeyboardInterrupt, SoftTimeLimitExceeded:
        interrupted = True
        raise
    finally:
        # 进程终止不证明远端调用结束，保留原租约至过期；正常释放也不解除业务 UNKNOWN 围栏。
        if not interrupted:
            try:
                release(redis_client, **scope)
            except RedisError, DomainError:
                logging.getLogger(__name__).warning(
                    "admission_release_failed",
                    extra={"tenant_id": str(tenant_id), "lease_id": str(lease_id)},
                )
