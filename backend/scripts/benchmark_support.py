"""Synthetic source inventory and external doubles for the capacity harness.

No business reader, resolver, draft, preview, or submission function is replaced.
Only external transports and the offline task-body prefork guard are substituted.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
from collections import Counter
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from random import Random
from time import perf_counter
from typing import Any, cast
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
from cryptography.fernet import Fernet
from redis import Redis
from sqlalchemy import func, insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session as SASession
from sqlmodel import Session, col, select
from urllib3.response import HTTPResponse

from app.core.config import settings
from app.core.context import TenantContext
from app.core.credentials import encrypt_credentials
from app.models import User
from app.modules.accounts.capabilities import (
    process_capability,
    start_capability_refresh,
)
from app.modules.accounts.capability_models import CapabilityJob
from app.modules.accounts.models import (
    AdvertiserAccount,
    BCAccountAccess,
    TenantBC,
    TikTokConnection,
)
from app.modules.accounts.router import get_accounts
from app.modules.builds.drafts import continue_draft, create_draft, prepare_draft
from app.modules.builds.execution_models import ExecutionStep, Submission
from app.modules.builds.models import BuildDraft, DraftPreparation
from app.modules.builds.preview_models import (
    BuildUnit,
    PlannedAd,
    PlannedGroup,
)
from app.modules.builds.previews import (
    continue_preview,
    generate_preview,
    get_preview_summary,
    get_preview_units,
)
from app.modules.builds.scene_job_models import SceneJob
from app.modules.builds.scene_jobs import ensure_scene_preparation, process_scene_job
from app.modules.builds.submissions import (
    expand_submission,
    get_submission,
    submit_preview,
)
from app.modules.materials.models import AccountMaterial, MaterialFile
from app.modules.providers.models import (
    LinkPreparationItem,
    PromotionLink,
    ProviderApplication,
    ProviderConnection,
    ProviderDrama,
)
from app.modules.providers.schemas import link_reuse_key
from app.modules.providers.tasks import process_item
from app.modules.strategies.copy_pool import POOL_VERSION
from app.modules.strategies.models import StrategyVersion
from app.modules.strategies.schemas import StrategyConfig
from app.modules.strategies.service import create_strategy
from app.modules.tenants.models import Tenant, TenantMembership
from scripts.benchmark_batches import Parameters, Recorder, measure_pages


@dataclass
class Scope:
    context: TenantContext
    user: User
    bc_id: str
    connection_id: UUID
    provider_id: UUID
    application_id: str
    strategy_version_id: UUID
    first_link: UUID
    account_prefix: str
    account_count: int

    def account(self, number: int) -> str:
        return f"{self.account_prefix}-{number:06}"


class Transport:
    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()
        self.scopes: dict[str, Scope] = {}

    def sdk(self, _pool: Any, method: str, url: str, **kwargs: Any) -> HTTPResponse:
        from urllib.parse import urlsplit

        path = urlsplit(url).path
        assert method == "GET", "Capacity harness forbids all TikTok writes"
        self.calls[path] += 1
        query = dict(kwargs.get("fields", []))
        if path.endswith("/bc/asset/get/"):
            assert "filtering" not in query
            scope = self.scopes[query["bc_id"]]
            page = int(query["page"])
            size = int(query["page_size"])
            start, end = (page - 1) * size, min(page * size, scope.account_count)
            data = {
                "list": [
                    {
                        "asset_id": scope.account(n),
                        "asset_type": "ADVERTISER",
                        "advertiser_role": "OPERATOR",
                    }
                    for n in range(start, end)
                ],
                "page_info": {
                    "page": page,
                    "page_size": size,
                    "total_page": (scope.account_count + 49) // 50,
                    "total_number": scope.account_count,
                },
            }
        elif path.endswith("/identity/get/"):
            data = {
                "identity_list": [
                    {
                        "identity_id": "capacity-identity",
                        "identity_type": "BC_AUTH_TT",
                        "identity_authorized_bc_id": query["identity_authorized_bc_id"],
                        "available_status": "AVAILABLE",
                        "can_push_video": True,
                        "is_gpppa": False,
                    }
                ],
                "page_info": {
                    "page": 1,
                    "page_size": 50,
                    "total_page": 1,
                    "total_number": 1,
                },
            }
        elif path.endswith("/minis/get/"):
            data = {
                "list": [
                    {
                        "minis_id": "capacity-minis",
                        "minis_status": "ACTIVE",
                        "minis_type": "MINI_SERIES",
                        "region_codes": ["US", "CA"],
                    }
                ],
                "page_info": {
                    "page": 1,
                    "page_size": 50,
                    "total_page": 1,
                    "total_number": 1,
                },
            }
        elif path.endswith("/creative/cta/recommend/"):
            data = {
                "recommend_assets": [
                    {"asset_ids": ["capacity-watch"], "asset_content": "Watch now"}
                ]
            }
        elif path.endswith("/tool/vbo_status/"):
            data = {"vo_min_roas": "QUALIFIED"}
        elif path.endswith("/tool/region/"):
            data = {
                "region_list": ["US", "CA"],
                "region_info": [
                    {
                        "region_code": "US",
                        "location_id": "6252001",
                        "level": "COUNTRY",
                        "area_type": "ADMIN",
                    },
                    {
                        "region_code": "CA",
                        "location_id": "6251999",
                        "level": "COUNTRY",
                        "area_type": "ADMIN",
                    },
                ],
            }
        else:
            raise AssertionError("Unrecognized SDK capacity read")
        return HTTPResponse(
            body=json.dumps(
                {"code": 0, "data": data, "request_id": "synthetic-capacity"}
            ).encode(),
            status=200,
        )

    def provider(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/getVideoList"), (
            "Only provider search reads are permitted"
        )
        self.calls["provider-search"] += 1
        payload = json.loads(request.content)
        title = payload["keywords"]
        number = int(title.rsplit(" ", 1)[1])
        return httpx.Response(
            200,
            json={
                "code": "0000",
                "data": {
                    "data": [
                        {"video_id": str(number), "name": title, "language": "en"}
                    ],
                    "count": 1,
                },
            },
        )


@contextmanager
def offline_runtime(transport: Transport) -> Iterator[Redis]:
    from app.jobs.admission import admission_keys
    from tests.database import require_test_redis

    redis_url = os.environ.get("TEST_REDIS_URL", "")
    require_test_redis(redis_url, settings.REDIS_URL)
    app_id = "capacity-" + uuid4().hex
    policy = {
        "base": {
            "app_max_inflight": 16,
            "endpoint_max_inflight": 8,
            "tenant_max_inflight": 16,
            "advertiser_max_inflight": 2,
            "app_calls_per_window": 100000,
            "endpoint_calls_per_window": 100000,
            "window_ms": 1000,
            "lease_ms": 60000,
        },
        "endpoints": {
            "/open_api/v1.3/file/video/ad/upload/": {
                "lease_ms": 970000,
                "endpoint_max_inflight": 1,
            }
        },
    }
    original_resolve = socket.getaddrinfo

    def local_only(host: str | None, *args: Any, **kwargs: Any) -> Any:
        if host not in {"localhost", "127.0.0.1", "::1", None}:
            raise RuntimeError(
                "External network is disabled during capacity measurement"
            )
        return original_resolve(host, *args, **kwargs)

    with ExitStack() as stack:
        stack.enter_context(
            patch.multiple(
                settings,
                TIKTOK_APP_ID=app_id,
                TIKTOK_APP_SECRET="synthetic-secret",
                TIKTOK_REDIRECT_URI="https://example.com/callback",
                CONNECTION_ENCRYPTION_KEY=Fernet.generate_key().decode(),
                TIKTOK_CALL_POLICIES=policy,
                S3_ENDPOINT_URL="http://127.0.0.1:19000",
                S3_BUCKET="capacity-offline",
                S3_ACCESS_KEY_ID="synthetic",
                S3_SECRET_ACCESS_KEY="synthetic",
                BC_CAPABILITY_MAX_AGE_SECONDS=type(settings)
                .model_fields["BC_CAPABILITY_MAX_AGE_SECONDS"]
                .default,
            )
        )
        stack.enter_context(
            patch(
                "urllib3.PoolManager.request",
                lambda pool, method, url, **kwargs: transport.sdk(
                    pool, method, url, **kwargs
                ),
            )
        )
        stack.enter_context(patch("socket.getaddrinfo", local_only))
        # Task bodies still execute every real claim/authority/nonce boundary. This
        # synchronous harness is not a benchmark of Celery prefork wallclock limits.
        stack.enter_context(
            patch(
                "app.modules.accounts.capabilities._require_bounded_worker",
                lambda: None,
            )
        )
        stack.enter_context(
            patch("app.modules.builds.scene_jobs._require_bounded_worker", lambda: None)
        )
        with Redis.from_url(redis_url, decode_responses=True) as redis_client:
            try:
                yield redis_client
            finally:
                prefix = admission_keys(app_id, "synthetic", uuid4(), "synthetic")[
                    0
                ].rsplit(":", 1)[0]
                batch: list[str] = []
                for key in redis_client.scan_iter(match=prefix + ":*", count=100):
                    batch.append(key)
                    if len(batch) == 100:
                        redis_client.delete(*batch)
                        batch.clear()
                if batch:
                    redis_client.delete(*batch)


def seed_inventory(engine: Engine, parameters: Parameters, label: str = "t1") -> Scope:
    random = Random(f"{parameters.seed}:{label}")
    with Session(engine, expire_on_commit=False) as session, session.begin():
        tenant, user = (
            Tenant(name="Synthetic capacity " + label),
            User(email=f"{uuid4()}@example.com", hashed_password="unused"),
        )
        session.add_all([tenant, user])
        session.flush()
        context = TenantContext(tenant_id=tenant.id, actor_id=user.id, role="operator")
        session.add(
            TenantMembership(tenant_id=tenant.id, user_id=user.id, role="operator")
        )
        bc = "capacity-bc-" + label
        session.add(TenantBC(tenant_id=tenant.id, bc_id=bc))
        conn = TikTokConnection(
            tenant_id=tenant.id,
            status="ACTIVE",
            credential_version=1,
            credential_ciphertext=encrypt_credentials(
                tenant_id=tenant.id,
                value={"access_token": "synthetic-token", "scope": "[2,6]"},
            ),
        )
        verification = uuid4()
        provider = ProviderConnection(
            tenant_id=tenant.id,
            kind="jiashu",
            display_name="Synthetic capacity",
            status="active",
            credential_version=1,
            verification_token=verification,
            encrypted_credentials=encrypt_credentials(
                tenant_id=tenant.id, value={"session": "synthetic-session"}
            ),
        )
        session.add_all([conn, provider])
        session.flush()
        app = ProviderApplication(
            tenant_id=tenant.id,
            connection_id=provider.id,
            external_id="capacity-app",
            name="Synthetic capacity",
            tiktok_minis_id="capacity-minis",
            channel_config={
                "channel_prefix": "capacity_",
                "verification_token": str(verification),
            },
        )
        session.add(app)
        strategy = create_strategy(
            session,
            context=context,
            name="Capacity strategy",
            config=StrategyConfig(
                budget=Decimal("100"),
                currency="USD",
                target_roas=Decimal("1.2"),
                group_size=parameters.group_size,
                creative_count=parameters.creative_count,
                copy_pool_version=POOL_VERSION,
            ),
        )
        version = session.exec(
            select(StrategyVersion).where(StrategyVersion.strategy_id == strategy)
        ).one()
        scope = Scope(
            context,
            user,
            bc,
            conn.id,
            provider.id,
            app.external_id,
            version.id,
            uuid4(),
            "capacity-" + label,
            parameters.accounts,
        )
    for offset in range(0, parameters.accounts, 1000):
        with Session(engine) as session, session.begin():
            SASession.execute(
                session,
                insert(AdvertiserAccount),
                [
                    {
                        "tenant_id": context.tenant_id,
                        "advertiser_id": scope.account(n),
                        "name": f"Capacity account {n:06}",
                        "currency": "USD",
                        "timezone": "UTC",
                        "remote_status": "ENABLE",
                        "ownership_conflict": False,
                    }
                    for n in range(offset, min(offset + 1000, parameters.accounts))
                ],
            )
            SASession.execute(
                session,
                insert(BCAccountAccess),
                [
                    {
                        "tenant_id": context.tenant_id,
                        "bc_id": bc,
                        "advertiser_id": scope.account(n),
                        "connection_id": conn.id,
                        "in_bc": True,
                        "authorized": True,
                        "active": True,
                        "permission_state": "UNKNOWN",
                        "can_build": False,
                        "can_upload": False,
                    }
                    for n in range(offset, min(offset + 1000, parameters.accounts))
                ],
            )
    for number in range(parameters.dramas):
        with Session(engine) as session, session.begin():
            title = f"Capacity Drama {number:04}"
            drama = ProviderDrama(
                tenant_id=context.tenant_id,
                connection_id=provider.id,
                application_id=app.external_id,
                external_drama_id=str(number),
                title=title,
                language="en",
            )
            session.add(drama)
            session.flush()
            link = PromotionLink(
                tenant_id=context.tenant_id,
                connection_id=provider.id,
                application_id=app.external_id,
                drama_id=drama.id,
                reuse_key=link_reuse_key(
                    context.tenant_id,
                    provider.id,
                    app.external_id,
                    str(number),
                    {"episode": 1},
                ),
                config={"episode": 1},
                url=f"https://example.com/capacity/{number}",
                protected_base=f"capacity-base-{number:04}",
                status="ready",
                verified_at=datetime.now(UTC),
            )
            session.add(link)
            session.flush()
            if number == 0:
                scope.first_link = link.id
            for material_number in range(parameters.materials_per_drama):
                identity = UUID(int=random.getrandbits(128), version=4)
                value = MaterialFile(
                    id=identity,
                    tenant_id=context.tenant_id,
                    bc_id=bc,
                    file_name=f"{title} - {material_number:03}.mp4",
                    object_key=f"synthetic/{identity}",
                    byte_size=1024,
                    sha256=hashlib.sha256(str(identity).encode()).hexdigest(),
                    video_md5=hashlib.md5(str(identity).encode()).hexdigest(),
                    storage_state="stored",
                )
                session.add(value)
                session.flush()
                session.add(
                    AccountMaterial(
                        tenant_id=context.tenant_id,
                        bc_id=bc,
                        material_id=value.id,
                        advertiser_id=scope.account(0),
                        connection_id=conn.id,
                        video_id=f"synthetic-{number}-{material_number}",
                        status="available",
                        verified_at=datetime.now(UTC),
                    )
                )
    return scope


def bootstrap(
    engine: Engine,
    scope: Scope,
    target_count: int,
    redis_client: Redis,
    recorder: Recorder,
) -> None:
    started = perf_counter()
    with Session(engine) as session, session.begin():
        identity = start_capability_refresh(
            session,
            context=scope.context,
            bc_id=scope.bc_id,
            connection_id=scope.connection_id,
            request_id=uuid4(),
        )
    iterations = 0
    while True:
        with Session(engine) as session:
            job = session.get(CapabilityJob, identity)
            assert job is not None
            if job.status != "PENDING":
                assert job.status == "COMPLETE", job.error_code
                break
            if job.error_code:
                raise AssertionError(f"Capability benchmark stopped: {job.error_code}")
            revision = job.revision
        process_capability(
            database_engine=engine,
            redis_client=redis_client,
            tenant_id=scope.context.tenant_id,
            actor_id=scope.context.actor_id,
            payload={"job_id": str(identity), "revision": revision},
        )
        iterations += 1
        if iterations % 100 == 0:
            recorder.progress(
                "capability_progress",
                units=iterations,
                seconds=perf_counter() - started,
            )
    recorder.progress(
        "capability",
        units=iterations,
        seconds=perf_counter() - started,
        configured_age_seconds=settings.BC_CAPABILITY_MAX_AGE_SECONDS,
        minimum_default_cadence_seconds=iterations * 5,
    )
    started = perf_counter()
    for number in range(target_count):
        with Session(engine) as session, session.begin():
            prep = ensure_scene_preparation(
                session,
                context=scope.context,
                bc_id=scope.bc_id,
                advertiser_id=scope.account(number),
                link_id=scope.first_link,
            )
        assert prep.job_id
        for _ in range(10):
            with Session(engine) as session:
                scene_job = session.get(SceneJob, prep.job_id)
                assert scene_job is not None
                if scene_job.status != "PENDING":
                    assert scene_job.status == "COMPLETE", scene_job.error_code
                    break
                revision = scene_job.revision
            process_scene_job(
                database_engine=engine,
                redis_client=redis_client,
                tenant_id=scope.context.tenant_id,
                actor_id=scope.context.actor_id,
                payload={"job_id": str(scene_job.id), "revision": revision},
            )
        with Session(engine) as session, session.begin():
            assert (
                ensure_scene_preparation(
                    session,
                    context=scope.context,
                    bc_id=scope.bc_id,
                    advertiser_id=scope.account(number),
                    link_id=scope.first_link,
                ).state
                == "ready"
            )
        if (number + 1) % 100 == 0:
            recorder.progress(
                "scene_progress", accounts=number + 1, seconds=perf_counter() - started
            )
    recorder.progress("scene", accounts=target_count, seconds=perf_counter() - started)


def prepare(
    engine: Engine,
    scope: Scope,
    parameters: Parameters,
    transport: Transport,
    recorder: Recorder,
) -> UUID:
    started = perf_counter()
    with Session(engine) as session, session.begin():
        draft_id = create_draft(
            session,
            context=scope.context,
            bc_id=scope.bc_id,
            strategy_version_id=scope.strategy_version_id,
            provider_connection_id=scope.provider_id,
            application_id=scope.application_id,
            drama_lines=[f"Capacity Drama {n:04}" for n in range(parameters.dramas)],
            account_lines=[scope.account(n) for n in range(parameters.target_accounts)],
            link_config={"episode": 1},
        )
        preparation_id = prepare_draft(
            session, context=scope.context, draft_id=draft_id, request_id=uuid4()
        )
        preparation = session.get(DraftPreparation, preparation_id)
        assert preparation is not None
        provider_id = preparation.provider_task_id
    after = None
    while True:
        with Session(engine) as session:
            statement = select(LinkPreparationItem).where(
                LinkPreparationItem.preparation_id == provider_id
            )
            if after:
                statement = statement.where(LinkPreparationItem.id > after)
            items = session.exec(
                statement.order_by(col(LinkPreparationItem.id)).limit(100)
            ).all()
        if not items:
            break
        for item in items:
            for _ in range(5):
                with Session(engine) as session:
                    current = session.get(LinkPreparationItem, item.id)
                    assert current is not None
                    if current.status == "ready":
                        break
                    revision = current.resolved.get("_work", {}).get(
                        "dispatch_revision", 0
                    )
                process_item(
                    database_engine=engine,
                    tenant_id=scope.context.tenant_id,
                    actor_id=scope.context.actor_id,
                    payload={"item_id": str(item.id), "revision": revision},
                    transport=httpx.MockTransport(transport.provider),
                )
            else:
                raise AssertionError(
                    "Provider preparation did not finish through normal local reuse"
                )
        after = items[-1].id
    iterations = 0
    while True:
        with Session(engine) as session, session.begin():
            done = continue_draft(
                session, context=scope.context, task_id=preparation_id
            )
        iterations += 1
        if done:
            break
        if iterations % 100 == 0:
            recorder.progress(
                "draft_progress", units=iterations, seconds=perf_counter() - started
            )
    with Session(engine) as session:
        draft = session.get(BuildDraft, draft_id)
        assert draft is not None and draft.status == "READY"
    recorder.progress("draft", units=iterations, seconds=perf_counter() - started)
    return draft_id


def freeze(engine: Engine, scope: Scope, draft_id: UUID, recorder: Recorder) -> UUID:
    started = perf_counter()
    with Session(engine) as session, session.begin():
        preview_id = generate_preview(
            session, context=scope.context, draft_id=draft_id, expected_revision=1
        )
    calls = 0
    while True:
        with Session(engine) as session, session.begin():
            done = continue_preview(
                session, context=scope.context, preview_id=preview_id
            )
        calls += 1
        if done:
            break
        if calls % 10 == 0:
            with Session(engine) as session:
                count = session.exec(
                    select(func.count())
                    .select_from(BuildUnit)
                    .where(BuildUnit.preview_id == preview_id)
                ).one()
            recorder.progress(
                "preview_progress",
                calls=calls,
                units=count,
                seconds=perf_counter() - started,
            )
    with Session(engine) as session:
        summary = get_preview_summary(
            session, context=scope.context, preview_id=preview_id
        )
    assert summary.status == "FROZEN"
    recorder.progress(
        "preview",
        calls=calls,
        seconds=perf_counter() - started,
        summary=summary.model_dump(mode="json"),
    )
    return preview_id


def run_scenario(
    engine: Engine, parameters: Parameters, recorder: Recorder, *, directory_only: bool
) -> None:
    transport = Transport()
    with offline_runtime(transport) as redis_client:
        started = perf_counter()
        scope = seed_inventory(engine, parameters)
        transport.scopes[scope.bc_id] = scope
        recorder.progress(
            "seed",
            seconds=perf_counter() - started,
            accounts=parameters.accounts,
            materials=parameters.dramas * parameters.materials_per_drama,
        )

        def account_page(cursor: str | None) -> Any:
            with Session(engine) as session:
                return get_accounts(
                    tenant_id=scope.context.tenant_id,
                    session=session,
                    user=scope.user,
                    bc_id=scope.bc_id,
                    cursor=cursor,
                    limit=100,
                )

        metrics = measure_pages(account_page, identity=lambda row: row.advertiser_id)
        assert metrics["rows"] == parameters.accounts
        recorder.progress("directory", **metrics)
        if directory_only:
            return
        bootstrap(engine, scope, parameters.target_accounts, redis_client, recorder)
        draft_id = prepare(engine, scope, parameters, transport, recorder)
        preview_id = freeze(engine, scope, draft_id, recorder)

        def preview_page(cursor: str | None) -> Any:
            with Session(engine) as session:
                return get_preview_units(
                    session,
                    context=scope.context,
                    preview_id=preview_id,
                    cursor=cursor,
                    limit=100,
                )

        metrics = measure_pages(preview_page, identity=lambda row: str(row.unit_id))
        expected = parameters.dramas * parameters.target_accounts
        assert metrics["rows"] == expected
        recorder.progress("preview_pages", **metrics)
        with Session(engine) as session:
            counts = {
                name: session.exec(
                    select(func.count())
                    .select_from(model)
                    .where(cast(Any, model).preview_id == preview_id)
                ).one()
                for name, model in [
                    ("campaign", BuildUnit),
                    ("group", PlannedGroup),
                    ("ad", PlannedAd),
                ]
            }
            summary = get_preview_summary(
                session, context=scope.context, preview_id=preview_id
            )
        group_count = (
            parameters.materials_per_drama + parameters.group_size - 1
        ) // parameters.group_size
        assert counts == {
            "campaign": expected,
            "group": expected * group_count,
            "ad": expected * group_count * parameters.creative_count,
        }
        assert (
            summary.daily_budget_sum == str(expected * 100)
            or float(summary.daily_budget_sum) == expected * 100
        )
        recorder.report["plan_counts"] = counts
        started = perf_counter()
        with Session(engine) as session, session.begin():
            receipt = submit_preview(
                session,
                context=scope.context,
                preview_id=preview_id,
                request_id=uuid4(),
            )
        calls = 0
        while True:
            with Session(engine) as session, session.begin():
                done = expand_submission(
                    session, context=scope.context, submission_id=receipt.submission_id
                )
            calls += 1
            if done:
                break
            if calls % 1000 == 0:
                recorder.progress(
                    "submission_progress", calls=calls, seconds=perf_counter() - started
                )
        with Session(engine) as session:
            submission = session.get(Submission, receipt.submission_id)
            assert submission is not None and submission.expanded
            steps = dict(
                session.exec(
                    select(ExecutionStep.kind, func.count())
                    .where(ExecutionStep.submission_id == receipt.submission_id)
                    .group_by(ExecutionStep.kind)
                ).all()
            )
            view = get_submission(
                session, context=scope.context, submission_id=receipt.submission_id
            )
        assert (
            steps["CAMPAIGN"] == expected
            and steps["ADGROUP"] == counts["group"]
            and steps["AD"] == counts["ad"]
        )
        recorder.progress(
            "submission",
            calls=calls,
            seconds=perf_counter() - started,
            steps=steps,
            summary=view.model_dump(mode="json"),
        )
        recorder.report["transport_call_counts"] = dict(transport.calls)
        from scripts.benchmark_runtime import measure_runtime

        recorder.progress(
            "runtime", **measure_runtime(engine, redis_client, transport, scope)
        )
