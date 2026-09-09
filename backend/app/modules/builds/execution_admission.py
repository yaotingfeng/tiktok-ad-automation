"""Tenant fairness over the shared App quota, held through SDK client cleanup."""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

from redis.exceptions import RedisError

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.sdk import SDK_SCOPE_INTERRUPTS, AccountAdmissionDeferred
from app.jobs.admission import admission_policy, admit_call, release_call
from app.modules.builds.fairness import finish_fair_turn, take_fair_turn

HARD_LIMIT = 45


@contextmanager
def admitted_build_call(
    redis_client: Any, *, context: TenantContext, endpoint: str, advertiser_id: str
) -> Iterator[None]:
    settings.require_tiktok_app()
    policy = admission_policy(endpoint)
    if policy.lease_ms <= (HARD_LIMIT + 5) * 1000:
        raise DomainError("admission_policy_invalid", "调用租约短于执行硬期限")
    app_scope, owner = settings.TIKTOK_APP_ID, uuid4()
    if not take_fair_turn(
        redis_client,
        app_scope=app_scope,
        tenant_id=context.tenant_id,
        owner=owner,
        wait_ms=30000,
        turn_ms=3000,
    ):
        raise AccountAdmissionDeferred(1000)
    granted, turn_finished, interrupted = False, False, False
    try:
        admission = admit_call(
            redis_client,
            app_scope=app_scope,
            endpoint=endpoint,
            tenant_id=context.tenant_id,
            advertiser_id=advertiser_id,
            lease_id=owner,
            policy=policy,
        )
        granted = admission.granted
        finish_fair_turn(
            redis_client,
            app_scope=app_scope,
            tenant_id=context.tenant_id,
            owner=owner,
            served=granted,
            retry_after_ms=admission.retry_after_ms,
        )
        turn_finished = True
        if not granted:
            raise AccountAdmissionDeferred(admission.retry_after_ms)
        yield
    except SDK_SCOPE_INTERRUPTS:
        interrupted = True
        raise
    finally:
        if not turn_finished:
            try:
                finish_fair_turn(
                    redis_client,
                    app_scope=app_scope,
                    tenant_id=context.tenant_id,
                    owner=owner,
                    served=False,
                    retry_after_ms=1000,
                )
            except RedisError, DomainError:
                pass  # Its short token-fenced TTL is the fallback; no SDK ran.
        if granted and not interrupted:
            try:
                release_call(
                    redis_client,
                    app_scope=app_scope,
                    endpoint=endpoint,
                    tenant_id=context.tenant_id,
                    advertiser_id=advertiser_id,
                    lease_id=owner,
                )
            except RedisError, DomainError:
                logging.getLogger(__name__).warning(
                    "build_admission_release_failed",
                    extra={"tenant_id": str(context.tenant_id), "lease_id": str(owner)},
                )
