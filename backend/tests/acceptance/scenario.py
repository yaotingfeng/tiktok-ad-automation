"""Real modules, transactions and official SDK; only external wire is synthetic.

Importable by the browser test server. No draft/link/scene/material/preview READY
rows are injected. Task bodies run after the real outbox publisher commits.
"""

from __future__ import annotations

import hashlib
import importlib
import io
import json
import os
import socket
import time
from collections import Counter, deque
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from urllib.parse import unquote, urlsplit
from uuid import UUID, uuid4

import httpx
from botocore.awsrequest import AWSResponse
from cryptography.fernet import Fernet
from redis import Redis
from sqlmodel import Session, select
from urllib3.response import HTTPResponse

from app.core.config import settings
from app.core.context import TenantContext
from app.core.credentials import encrypt_credentials
from app.core.security import get_password_hash
from app.jobs.celery_app import celery_app
from app.models import User
from app.modules.accounts.models import (
    AdvertiserAccount,
    BCAccountAccess,
    TenantBC,
    TikTokConnection,
)
from app.modules.builds import drafts, previews, submissions
from app.modules.builds.models import BuildDraft, DraftPreparation
from app.modules.builds.preview_models import BuildPreview
from app.modules.materials.models import (
    AccountMaterial,
    MaterialAssetOperation,
    MaterialFile,
    MaterialUploadAttempt,
)
from app.modules.materials.storage import object_key_for
from app.modules.providers.models import ProviderApplication, ProviderConnection
from app.modules.strategies.copy_pool import POOL_VERSION
from app.modules.strategies.models import StrategyVersion
from app.modules.strategies.schemas import StrategyConfig
from app.modules.strategies.service import create_strategy
from app.modules.tenants.models import Tenant, TenantMembership
from tests.fakes.tiktok import FakeTikTokAPI

DRAMAS = ("The Bond", "Hidden Promise")
PASSWORD = "Offline-acceptance-2026!"


def synthetic_id(key: str) -> str:
    """Stable 20-digit opaque identifier, above JavaScript's safe integer range."""
    return str(10**19 + int(hashlib.sha256(key.encode()).hexdigest()[:14], 16))


@dataclass
class Scope:
    context: TenantContext
    email: str
    bc_id: str
    connection_id: UUID
    provider_id: UUID
    application_id: str
    version_id: UUID
    accounts: tuple[str, ...]
    sources: tuple[str, ...]
    label: str = ""
    material_ids: list[UUID] = field(default_factory=list)
    admin_email: str = ""
    admin_id: UUID | None = None
    platform_email: str = ""
    platform_id: UUID | None = None


class Wire:
    """Closed provider/S3/TikTok transport; unsupported endpoints fail loudly."""

    def __init__(self) -> None:
        self.smart = FakeTikTokAPI()
        self.scopes: dict[str, Scope] = {}
        self.objects: dict[str, tuple[bytes, UUID, UUID]] = {}
        self.videos: dict[tuple[str, str], dict[str, Any]] = {}
        self.portfolios: dict[str, dict[str, Any]] = {}
        self.images: dict[tuple[str, str], dict[str, Any]] = {}
        self.calls: Counter[str] = Counter()
        self.minis_unavailable_accounts: set[str] = set()
        self.lose_response_kind: str | None = None
        self.ambiguous_kind: str | None = None
        self.after_create: Callable[[str, dict[str, Any]], None] | None = None
        self.before_get: Callable[[str], None] | None = None

    def sdk(self, _pool: Any, method: str, url: str, **kwargs: Any) -> HTTPResponse:
        path = urlsplit(url).path
        self.calls[path] += 1
        if method == "GET" and self.before_get:
            self.before_get(path)
        query = dict(kwargs.get("fields") or [])
        body = (
            json.loads(kwargs.get("body") or "{}")
            if method == "POST" and not query
            else query
        )
        account = body.get("advertiser_id") or query.get("advertiser_id")
        scopes = [
            scope
            for scope in self.scopes.values()
            if (
                account in scope.accounts + scope.sources
                if account
                else scope.bc_id == query.get("bc_id")
            )
        ]
        if (
            len(scopes) != 1
            or kwargs.get("headers", {}).get("Access-Token")
            != "synthetic-" + scopes[0].label
        ):
            raise AssertionError("TikTok wire rejected a tenant/credential mismatch")
        if "/smart_plus/" in path:
            response = self.smart.call_api(
                path, method, body=body, query_params=list(query.items())
            )
            data = response.data
            kind = path.split("/")[4]
            if method == "GET" and kind == "adgroup":
                # The Smart+ adgroup get contract does not supply operation_status.
                # Force the production independent standard adgroup GET fallback.
                for row in data["list"]:
                    row.pop("operation_status", None)
            if method == "POST":
                if self.after_create:
                    self.after_create(kind, data)
                if self.lose_response_kind == kind:
                    self.lose_response_kind = None
                    if self.ambiguous_kind == kind:
                        from copy import deepcopy

                        clone = deepcopy(data)
                        key = {
                            "campaign": "campaign_id",
                            "adgroup": "adgroup_id",
                            "ad": "smart_plus_ad_id",
                        }[kind]
                        clone[key] = "99999999"
                        self.smart.store[kind][clone[key]] = clone
                    raise TimeoutError("Synthetic response lost after remote commit")
        elif path.endswith("/bc/asset/get/"):
            scope = self.scopes[query["bc_id"]]
            accounts = scope.accounts + scope.sources
            page, size = int(query["page"]), int(query["page_size"])
            data = {
                "list": [
                    {
                        "asset_id": value,
                        "asset_type": "ADVERTISER",
                        "advertiser_role": "OPERATOR",
                    }
                    for value in accounts[(page - 1) * size : page * size]
                ],
                "page_info": self.page(page, size, len(accounts)),
            }
        elif path.endswith("/identity/get/"):
            data = {
                "identity_list": [
                    {
                        "identity_id": "acceptance-identity",
                        "identity_type": "BC_AUTH_TT",
                        "identity_authorized_bc_id": query["identity_authorized_bc_id"],
                        "available_status": "AVAILABLE",
                        "can_push_video": True,
                        "is_gpppa": False,
                    }
                ],
                "page_info": self.page(1, 50, 1),
            }
        elif path.endswith("/minis/get/"):
            data = {
                "list": [
                    {
                        "minis_id": "acceptance-minis",
                        "minis_status": "ACTIVE",
                        "minis_type": "MINI_SERIES",
                        "region_codes": ["US", "CA"],
                    }
                ],
                "page_info": self.page(1, 50, 1),
            }
            if query["advertiser_id"] in self.minis_unavailable_accounts:
                data = {"list": [], "page_info": self.page(1, 50, 0)}
        elif path.endswith("/creative/cta/recommend/"):
            data = {
                "recommend_assets": [
                    {"asset_ids": ["acceptance-watch"], "asset_content": "Watch now"}
                ]
            }
        elif path.endswith("/tool/vbo_status/"):
            data = {"vo_min_roas": "QUALIFIED"}
        elif path.endswith("/tool/region/"):
            data = {
                "region_list": ["US", "CA"],
                "region_info": [
                    {
                        "region_code": name,
                        "location_id": identity,
                        "level": "COUNTRY",
                        "area_type": "ADMIN",
                    }
                    for name, identity in [("US", "6252001"), ("CA", "6251999")]
                ],
            }
        elif path.endswith("/file/video/ad/upload/"):
            assert method == "POST" and query["upload_type"] == "UPLOAD_BY_FILE"
            video_id = f"target-vid-{len(self.videos)}"
            row = {
                "video_id": video_id,
                "material_id": f"target-mid-{len(self.videos)}",
                "signature": query["video_signature"],
                "displayable": True,
                "width": 720,
                "height": 1280,
                "video_cover_url": f"https://p16.example.com/cover-{len(self.videos)}.jpeg",
                "file_name": query["file_name"],
            }
            self.videos[(query["advertiser_id"], video_id)] = row
            data = [{"video_id": video_id, "material_id": row["material_id"]}]
        elif path.endswith("/file/video/ad/info/"):
            ids = (
                json.loads(query["video_ids"])
                if isinstance(query["video_ids"], str)
                else query["video_ids"]
            )
            data = {
                "list": [
                    self.videos[(query["advertiser_id"], identity)]
                    for identity in ids
                    if (query["advertiser_id"], identity) in self.videos
                ]
            }
        elif path.endswith("/file/image/ad/upload/"):
            assert method == "POST" and body["upload_type"] == "UPLOAD_BY_URL"
            assert body["image_url"].startswith("https://p16.example.com/cover-")
            image_id = f"target-image-{len(self.images)}"
            data = {
                "image_id": image_id,
                "file_name": body["file_name"],
                "displayable": True,
                "width": 720,
                "height": 1280,
                "signature": hashlib.md5(body["image_url"].encode()).hexdigest(),
            }
            self.images[(body["advertiser_id"], image_id)] = data
        elif path.endswith("/file/image/ad/info/"):
            ids = (
                json.loads(query["image_ids"])
                if isinstance(query["image_ids"], str)
                else query["image_ids"]
            )
            data = {
                "list": [
                    self.images[(query["advertiser_id"], identity)]
                    for identity in ids
                    if (query["advertiser_id"], identity) in self.images
                ]
            }
        elif path.endswith("/file/image/ad/search/"):
            assert not query.get("filtering")
            rows = [
                row
                for (account, _), row in self.images.items()
                if account == query["advertiser_id"]
            ]
            page, size = int(query["page"]), int(query["page_size"])
            data = {
                "list": rows[(page - 1) * size : page * size],
                "page_info": self.page(page, size, len(rows)),
            }
        elif path.endswith("/creative/portfolio/create/"):
            identity = str(800000 + len(self.portfolios))
            self.portfolios[identity] = {**body, "creative_portfolio_id": identity}
            data = {"creative_portfolio_id": identity}
        elif path.endswith("/creative/portfolio/get/"):
            data = self.portfolios[query["creative_portfolio_id"]]
        elif path.endswith("/adgroup/get/"):
            filters = json.loads(query["filtering"])
            assert set(filters) == {"campaign_ids", "adgroup_ids"}
            rows = [
                row
                for row in self.smart.store["adgroup"].values()
                if row["advertiser_id"] == query["advertiser_id"]
                and row["adgroup_id"] in filters["adgroup_ids"]
                and row["campaign_id"] in filters["campaign_ids"]
            ]
            data = {
                "list": rows,
                "page_info": self.page(1, int(query["page_size"]), len(rows)),
            }
        else:
            raise AssertionError(f"Unrecognized TikTok wire operation: {method} {path}")
        return HTTPResponse(
            body=json.dumps(
                {"code": 0, "data": data, "request_id": "offline-acceptance"}
            ).encode(),
            status=200,
        )

    @staticmethod
    def page(page: int, size: int, count: int) -> dict[str, int]:
        return {
            "page": page,
            "page_size": size,
            "total_number": count,
            "total_page": (count + size - 1) // size,
        }

    def provider(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls["provider:" + path] += 1
        application = request.url.params.get("app") or request.url.params.get("channel")
        scopes = [
            scope
            for scope in self.scopes.values()
            if scope.application_id == application
        ]
        if len(scopes) != 1:
            raise AssertionError("Provider wire rejected an unknown application scope")
        label = scopes[0].label
        if request.url.host == "partners.shortswave.com":
            assert path.endswith("/drama/list")
            if request.headers.get("cookie") != "x-ds-admin-token=synthetic-" + label:
                raise AssertionError("Provider wire rejected mismatched credentials")
            page = int(request.url.params["page"])
            title = request.url.params["title"]
            rows = (
                [{"id": str(DRAMAS.index(title) + 1), "title": title, "lang": "en"}]
                if page == 1
                else []
            )
            return httpx.Response(200, json={"code": 0, "data": rows})
        assert (
            request.url.host == "video-wechat-open.eastdrama.net"
            and request.method == "POST"
        )
        if request.headers.get("session") != "synthetic-" + label:
            raise AssertionError("Provider wire rejected mismatched credentials")
        body = json.loads(request.content)
        if path.endswith("/getVideoList"):
            title = body["keywords"]
            data = {
                "data": [
                    {
                        "video_id": str(DRAMAS.index(title) + 1),
                        "name": title,
                        "language": "en",
                    }
                ],
                "count": 1,
            }
        elif path.endswith("/getChannelList"):
            data = {
                "data": [{"channel": body["channel"], "remark": "Acceptance history"}],
                "count": 1,
            }
        elif path.endswith("/getGuideUrl"):
            channel = body["channel"]
            vid = channel.rsplit("_", 1)[1]
            data = {
                "config": {
                    "vid": vid,
                    "drama_num": 1,
                    "charge_level": "1",
                    "jump_url": f"https://www.tiktok.com/minis/acceptance?channel={channel}&vid={vid}&dramaNum=1&charge_level=1",
                    "minis_path": "pages/drama",
                }
            }
        else:
            raise AssertionError(f"Unrecognized provider wire: {path}")
        return httpx.Response(200, json={"code": "0000", "data": data})

    def s3(self, _session: Any, request: Any) -> AWSResponse:
        assert request.method == "GET"
        key = unquote(urlsplit(request.url).path).removeprefix("/acceptance-offline/")
        data, tenant_id, material_id = self.objects[key]
        self.calls["s3:get_object"] += 1

        class Stream(io.BytesIO):
            def stream(self, amt: int = 1024, **_kwargs: Any) -> Iterator[bytes]:
                while block := self.read(amt):
                    yield block

        return AWSResponse(
            request.url,
            200,
            {
                "content-length": str(len(data)),
                "x-amz-meta-tenant-id": str(tenant_id),
                "x-amz-meta-material-id": str(material_id),
            },
            Stream(data),
        )


@dataclass
class Runtime:
    wire: Wire
    database_engine: Any
    messages: deque = field(default_factory=deque)
    delivered: list[dict[str, Any]] = field(default_factory=list)

    def publish(self, name: str, **kwargs: Any) -> SimpleNamespace:
        message = {"name": name, **kwargs}
        self.messages.append(message)
        return SimpleNamespace(id=kwargs["task_id"])

    def deliver(self, message: dict[str, Any]) -> None:
        task = celery_app.tasks[message["name"]]
        # Only the production execution-environment guard is substituted. The
        # registered original task function still validates payload and identity.
        task.push_request(
            id=message["task_id"],
            called_directly=False,
            is_eager=False,
            timelimit=(task.time_limit, task.soft_time_limit),
        )
        try:
            task.run(**message["kwargs"])
        finally:
            task.pop_request()
        self.delivered.append(message)

    def step_job(self) -> bool:
        """Run at most one actual delivery; useful for crash-boundary barriers."""
        from app.jobs.outbox import flush_dispatch

        flush_dispatch(limit=100)
        if not self.messages:
            return False
        self.deliver(self.messages.popleft())
        return True

    def pump_jobs(self, max_steps: int = 1000) -> int:
        from app.jobs.outbox import flush_dispatch

        for count in range(max_steps):
            flush_dispatch(limit=100)
            if not self.messages:
                return count
            self.deliver(self.messages.popleft())
        raise AssertionError(
            f"Task pump exceeded {max_steps}; pending task IDs: {[m['task_id'] for m in self.messages]}"
        )

    def drive_until(self, predicate: Callable[[], bool], timeout: float = 240) -> None:
        deadline = time.monotonic() + timeout
        while not predicate():
            self.pump_jobs()
            if predicate():
                return
            if time.monotonic() >= deadline:
                raise AssertionError(
                    "Acceptance workflow did not settle: " + self.diagnostics()
                )
            time.sleep(0.1)

    def diagnostics(self) -> str:
        from app.modules.builds.execution_models import ExecutionStep
        from app.modules.materials.cover_models import MaterialCoverJob
        from app.modules.materials.models import MaterialDistribution

        with Session(self.database_engine) as session:
            return repr(
                {
                    "drafts": [
                        (r.status, r.phase, r.error_code)
                        for r in session.exec(select(DraftPreparation))
                    ],
                    "steps": Counter(
                        (r.kind, r.status, r.error_code)
                        for r in session.exec(select(ExecutionStep))
                    ),
                    "wire": dict(self.wire.calls),
                    "covers": Counter(
                        (r.status, r.error_code)
                        for r in session.exec(select(MaterialCoverJob))
                    ),
                    "materials": Counter(
                        (r.status, r.reason_code)
                        for r in session.exec(select(MaterialDistribution))
                    ),
                }
            )


@contextmanager
def offline_runtime(wire: Wire, database_engine: Any) -> Iterator[Runtime]:
    """Reusable by a real FastAPI server; installs no business-service doubles."""
    from app.jobs.admission import admission_keys
    from tests.database import require_test_redis

    redis_url = os.environ.get("TEST_REDIS_URL", "")
    require_test_redis(redis_url, settings.REDIS_URL)
    runtime = Runtime(wire, database_engine)
    app_id = "acceptance-" + uuid4().hex
    original_resolve = socket.getaddrinfo

    def local_only(host: str | None, *args: Any, **kwargs: Any) -> Any:
        if host not in {"localhost", "127.0.0.1", "::1", None}:
            raise AssertionError("Acceptance tests forbid external DNS/network")
        return original_resolve(host, *args, **kwargs)

    with ExitStack() as stack:
        stack.enter_context(
            patch.multiple(
                settings,
                TIKTOK_APP_ID=app_id,
                TIKTOK_APP_SECRET="synthetic-secret",
                TIKTOK_REDIRECT_URI="https://example.com/callback",
                CONNECTION_ENCRYPTION_KEY=Fernet.generate_key().decode(),
                REDIS_URL=redis_url,
                S3_ENDPOINT_URL="http://127.0.0.1:19000",
                S3_BUCKET="acceptance-offline",
                S3_ACCESS_KEY_ID="synthetic",
                S3_SECRET_ACCESS_KEY="synthetic",
                TIKTOK_CALL_POLICIES={
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
                },
            )
        )
        for module_name in celery_app.conf.imports:
            module = importlib.import_module(module_name)
            if hasattr(module, "engine"):
                stack.enter_context(patch.object(module, "engine", database_engine))
        stack.enter_context(patch("app.jobs.outbox.engine", database_engine))
        stack.enter_context(patch.object(celery_app, "send_task", runtime.publish))
        stack.enter_context(
            patch(
                "urllib3.PoolManager.request",
                lambda pool, method, url, **kwargs: wire.sdk(
                    pool, method, url, **kwargs
                ),
            )
        )
        stack.enter_context(
            patch(
                "httpx.HTTPTransport.handle_request",
                lambda _transport, request: wire.provider(request),
            )
        )
        stack.enter_context(
            patch(
                "botocore.httpsession.URLLib3Session.send",
                lambda session, request: wire.s3(session, request),
            )
        )
        stack.enter_context(patch("socket.getaddrinfo", local_only))
        for target in [
            "accounts.capabilities._require_bounded_worker",
            "builds.scene_jobs._require_bounded_worker",
            "builds.execution._require_bounded_worker",
            "builds.reconciliation.require_bounded_worker",
            "materials.tasks.require_bounded_worker",
        ]:
            stack.enter_context(
                patch("app.modules." + target, lambda *args, **kwargs: None)
            )
        if "app.modules.materials.cover_tasks" in celery_app.conf.imports:
            stack.enter_context(
                patch(
                    "app.modules.materials.cover_tasks.require_bounded_worker",
                    lambda *args, **kwargs: None,
                )
            )
        stack.enter_context(
            patch(
                "app.modules.providers.tasks.current_process",
                lambda: SimpleNamespace(daemon=True, name="ForkPoolWorker-offline"),
            )
        )
        with Redis.from_url(redis_url, decode_responses=True) as redis_client:
            try:
                yield runtime
            finally:
                prefix = admission_keys(app_id, "synthetic", uuid4(), "synthetic")[
                    0
                ].rsplit(":", 1)[0]
                keys = list(redis_client.scan_iter(match=prefix + ":*", count=100))
                for offset in range(0, len(keys), 100):
                    redis_client.delete(*keys[offset : offset + 100])


def seed_scope(
    database_engine: Any,
    wire: Wire,
    *,
    label: str,
    provider_kind: str = "jiashu",
    material_count: int = 23,
) -> Scope:
    """Only identity, credentials, discovered accounts and original/source inventory."""
    with Session(database_engine) as session, session.begin():
        tenant = Tenant(name="Acceptance " + label)
        user = User(
            email=f"{label}-{uuid4().hex}@example.com",
            hashed_password=get_password_hash(PASSWORD),
            is_superuser=False,
        )
        admin = User(
            email=f"{label}-admin-{uuid4().hex}@example.com",
            hashed_password=get_password_hash(PASSWORD),
        )
        platform = User(
            email=f"{label}-platform-{uuid4().hex}@example.com",
            hashed_password=get_password_hash(PASSWORD),
            is_superuser=True,
        )
        session.add_all([tenant, user, admin, platform])
        session.flush()
        context = TenantContext(tenant_id=tenant.id, actor_id=user.id, role="operator")
        session.add(
            TenantMembership(tenant_id=tenant.id, user_id=user.id, role="operator")
        )
        session.add(
            TenantMembership(tenant_id=tenant.id, user_id=admin.id, role="tenant_admin")
        )
        bc = synthetic_id("bc:" + label)
        session.add(
            TenantBC(tenant_id=tenant.id, bc_id=bc, name="Acceptance BC " + label)
        )
        conn = TikTokConnection(
            tenant_id=tenant.id,
            status="ACTIVE",
            credential_version=1,
            credential_ciphertext=encrypt_credentials(
                tenant_id=tenant.id,
                value={"access_token": "synthetic-" + label, "scope": "[2,6]"},
            ),
        )
        verification = uuid4()
        provider = ProviderConnection(
            tenant_id=tenant.id,
            kind=provider_kind,
            display_name=provider_kind + " acceptance",
            status="active",
            credential_version=1,
            verification_token=verification,
            encrypted_credentials=encrypt_credentials(
                tenant_id=tenant.id,
                value={"session": "synthetic-" + label}
                if provider_kind == "jiashu"
                else {"token": "synthetic-" + label},
            ),
        )
        session.add_all([conn, provider])
        session.flush()
        app = ProviderApplication(
            tenant_id=tenant.id,
            connection_id=provider.id,
            external_id="acceptance-app-" + label,
            name="Acceptance Minis",
            tiktok_minis_id="acceptance-minis",
            channel_config={
                "channel_prefix": "acceptance_",
                "verification_token": str(verification),
            },
        )
        session.add(app)
        strategy_id = create_strategy(
            session,
            context=context,
            name="Acceptance K10 N2",
            config=StrategyConfig(
                budget=Decimal("100"),
                currency="USD",
                target_roas=Decimal("1.2"),
                group_size=10,
                creative_count=2,
                copy_pool_version=POOL_VERSION,
            ),
        )
        version = session.exec(
            select(StrategyVersion).where(StrategyVersion.strategy_id == strategy_id)
        ).one()
        scope = Scope(
            context,
            user.email,
            bc,
            conn.id,
            provider.id,
            app.external_id,
            version.id,
            tuple(synthetic_id(f"{label}:target:{n}") for n in range(3)),
            tuple(synthetic_id(f"{label}:source:{n}") for n in range(2)),
        )
        scope.label = label
        scope.admin_email, scope.admin_id = admin.email, admin.id
        scope.platform_email, scope.platform_id = platform.email, platform.id
        for i, advertiser in enumerate(scope.accounts + scope.sources):
            session.add(
                AdvertiserAccount(
                    tenant_id=tenant.id,
                    advertiser_id=advertiser,
                    name="Same display name" if i < 2 else "Account " + str(i),
                    currency="USD",
                    timezone="UTC",
                    remote_status="ENABLE",
                )
            )
        session.flush()
        for advertiser in scope.accounts + scope.sources:
            session.add(
                BCAccountAccess(
                    tenant_id=tenant.id,
                    bc_id=bc,
                    advertiser_id=advertiser,
                    connection_id=conn.id,
                    in_bc=True,
                    authorized=True,
                    active=True,
                    permission_state="UNKNOWN",
                    can_build=False,
                    can_upload=False,
                )
            )
        session.flush()
        for title in DRAMAS:
            for number in range(material_count):
                material_id = uuid4()
                key = object_key_for(tenant.id, material_id)
                contents = b"offline-video-fixture:" + str(material_id).encode()
                wire.objects[key] = (contents, tenant.id, material_id)
                material = MaterialFile(
                    id=material_id,
                    tenant_id=tenant.id,
                    bc_id=bc,
                    file_name=f"{title}-{number:03}.mp4",
                    object_key=key,
                    byte_size=len(contents),
                    sha256=hashlib.sha256(contents).hexdigest(),
                    video_md5=hashlib.md5(contents).hexdigest(),
                    storage_state="stored",
                )
                session.add(material)
                session.flush()
                session.add(
                    AccountMaterial(
                        tenant_id=tenant.id,
                        bc_id=bc,
                        material_id=material.id,
                        advertiser_id=scope.sources[number % 2],
                        connection_id=conn.id,
                        video_id="source-vid-" + str(material_id),
                        mid="source-mid-" + str(material_id),
                        status="available",
                        verified_at=datetime.now(UTC),
                    )
                )
                operation = MaterialAssetOperation(
                    tenant_id=tenant.id,
                    bc_id=bc,
                    material_id=material.id,
                    advertiser_id=scope.sources[number % 2],
                    path="upload_original",
                    status="succeeded",
                    request_digest=hashlib.sha256(contents).hexdigest(),
                    remote_response={
                        "video_id": "source-vid-" + str(material.id),
                        "mid": "source-mid-" + str(material.id),
                    },
                )
                session.add(operation)
                session.flush()
                session.add(
                    MaterialUploadAttempt(
                        tenant_id=tenant.id,
                        bc_id=bc,
                        material_id=material.id,
                        advertiser_id=scope.sources[number % 2],
                        connection_id=conn.id,
                        operation_id=operation.id,
                        status="succeeded",
                        request_digest=operation.request_digest,
                        remote_response=dict(operation.remote_response),
                    )
                )
                scope.material_ids.append(material.id)
    wire.scopes[bc] = scope
    return scope


@dataclass
class AcceptanceScenario:
    database_engine: Any
    runtime: Runtime
    scope: Scope
    other: Scope
    draft_id: UUID | None = None
    preview_id: UUID | None = None
    submission_id: UUID | None = None

    def prepare(self) -> UUID:
        with Session(self.database_engine) as session, session.begin():
            self.draft_id = drafts.create_draft(
                session,
                context=self.scope.context,
                bc_id=self.scope.bc_id,
                strategy_version_id=self.scope.version_id,
                provider_connection_id=self.scope.provider_id,
                application_id=self.scope.application_id,
                drama_lines=list(DRAMAS),
                account_lines=list(self.scope.accounts),
                link_config={"episode": 1},
            )
            preparation_id = drafts.prepare_draft(
                session,
                context=self.scope.context,
                draft_id=self.draft_id,
                request_id=uuid4(),
            )

        def done() -> bool:
            with Session(self.database_engine) as session:
                return session.get(DraftPreparation, preparation_id).status != "PENDING"

        self.runtime.drive_until(done)
        return preparation_id

    def freeze(self) -> UUID:
        assert self.draft_id
        with Session(self.database_engine) as session, session.begin():
            draft = session.get(BuildDraft, self.draft_id)
            assert draft.status == "READY", self.runtime.diagnostics()
            self.preview_id = previews.generate_preview(
                session,
                context=self.scope.context,
                draft_id=self.draft_id,
                expected_revision=draft.revision,
            )

        def done() -> bool:
            with Session(self.database_engine) as session:
                return session.get(BuildPreview, self.preview_id).status != "BUILDING"

        self.runtime.drive_until(done)
        return self.preview_id

    def submit(self, request_id: UUID | None = None) -> UUID:
        assert self.preview_id
        with Session(self.database_engine) as session, session.begin():
            receipt = submissions.submit_preview(
                session,
                context=self.scope.context,
                preview_id=self.preview_id,
                request_id=request_id or uuid4(),
            )
            self.submission_id = receipt.submission_id
        return self.submission_id

    def view(self) -> Any:
        with Session(self.database_engine) as session:
            return submissions.get_submission(
                session, context=self.scope.context, submission_id=self.submission_id
            )
