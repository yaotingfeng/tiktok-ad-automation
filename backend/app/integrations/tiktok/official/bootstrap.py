"""API 候选多 BC 读取工厂；未发布候选不构造虚假的 runtime route。"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import UUID

from redis import Redis
from sqlalchemy import Engine

from app.core.config import settings
from app.core.context import TenantContext
from app.core.credentials import decrypt_credentials
from app.core.errors import DomainError
from app.integrations.tiktok.admission import (
    ACCOUNT_DIRECTORY_OPERATIONS,
    admit_candidate_call,
    quota_scope,
)
from app.integrations.tiktok.bounded_resources import bounded_redis, bounded_session
from app.integrations.tiktok.contracts.accounts import CandidateReadContext
from app.integrations.tiktok.official.accounts import OfficialAccountsGateway
from app.integrations.tiktok.official.authorization import material_authorization
from app.integrations.tiktok.sdk import official_client
from app.jobs.admission import admission_policy
from app.modules.accounts.discovery import locked_run, validate_run
from app.modules.accounts.models import AuthorizationAttempt, DiscoveryRun


@contextmanager
def open_api_candidate_accounts(
    *,
    database_engine: Engine,
    redis_client: Redis,
    context: TenantContext,
    run_id: UUID,
    task_deadline: datetime,
) -> Iterator[OfficialAccountsGateway]:
    settings.require_tiktok_app()
    expected_claim: UUID | None = None

    def material() -> tuple[UUID, dict[str, str]]:
        with bounded_session(database_engine, task_deadline=task_deadline) as session:
            run = session.get(DiscoveryRun, run_id, populate_existing=True)
            if (
                run is None
                or (run.tenant_id, run.actor_id)
                != (context.tenant_id, context.actor_id)
                or (expected_claim is not None and run.claim_id != expected_claim)
                or run.candidate_attempt_id is None
                or run.status not in {"RUNNING", "ADMISSION_WAIT"}
            ):
                raise DomainError("discovery_stale", "候选目录任务已失效")
            connection = validate_run(session, run)
            if connection.kind != "OFFICIAL_API":
                raise DomainError("connection_channel_mismatch", "此候选不属于官方 API")
            attempt = session.get(AuthorizationAttempt, run.candidate_attempt_id)
            assert attempt is not None
            return attempt.id, decrypt_credentials(
                tenant_id=context.tenant_id,
                ciphertext=attempt.candidate_ciphertext or "",
            )

    attempt_id, private = material()
    with bounded_session(database_engine, task_deadline=task_deadline) as session:
        run = session.get(DiscoveryRun, run_id)
        assert run is not None
        expected_claim = run.claim_id
    token = private.get("access_token")
    if not token:
        raise DomainError("credential_invalid", "凭据缺少访问令牌")
    facts = material_authorization(private, observed_at=datetime.now(UTC))

    @contextmanager
    def request_scope(
        advertiser_id: str | None, operation: str, deadline: datetime
    ) -> Iterator[None]:
        if (
            advertiser_id is not None
            or operation not in ACCOUNT_DIRECTORY_OPERATIONS
            or deadline != task_deadline
        ):
            raise DomainError("gateway_operation_forbidden", "候选仅允许账户目录读取")
        material()
        policy = admission_policy(operation)
        if policy.lease_ms <= max(
            50_000, (task_deadline - datetime.now(UTC)).total_seconds() * 1000 + 1000
        ):
            raise DomainError(
                "admission_policy_invalid", "调用租约必须覆盖候选任务期限"
            )
        with bounded_redis(redis_client, task_deadline=task_deadline) as bounded:
            with admit_candidate_call(
                bounded,
                tenant_id=context.tenant_id,
                attempt_id=attempt_id,
                scope=quota_scope(
                    channel="OFFICIAL_API",
                    app_id=settings.TIKTOK_APP_ID,
                    verified_service_scope=None,
                ),
                operation=operation,
                policy=policy,
            ):
                material()
                with bounded_session(
                    database_engine, task_deadline=task_deadline
                ) as session:
                    run = locked_run(session, run_id)
                    validate_run(session, run)
                    if run.claim_id != expected_claim:
                        raise DomainError("discovery_stale", "候选调用的派发已失效")
                    run.sent_count += 1
                    session.add(run)
                    session.commit()
                yield

    try:
        with official_client(access_token=token) as client:
            yield OfficialAccountsGateway(
                client,
                context=CandidateReadContext(),
                authorization=facts,
                app_id=settings.TIKTOK_APP_ID,
                secret=settings.TIKTOK_APP_SECRET,
                request_scope=request_scope,
                deadline=task_deadline,
            )
    finally:
        private.clear()
