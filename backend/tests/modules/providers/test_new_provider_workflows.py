"""Durable end-to-end preparation checks for the three newly wired providers."""

# ruff: noqa: F811

from dataclasses import dataclass, field
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlmodel import Session, select

from app.core.config import settings
from app.core.credentials import encrypt_credentials
from app.modules.providers.link_steps import run_link_item
from app.modules.providers.models import (
    LinkPreparationItem,
    ProviderApplication,
    ProviderConnection,
)
from app.modules.providers.service import prepare_links
from tests.modules.strategies.test_concurrency import (  # noqa: F401
    isolated_strategy_database,
)


@dataclass
class ProviderRemote:
    kind: str
    calls: list[str] = field(default_factory=list)
    created: bool = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(f"{request.method} {path}")
        if self.kind == "duiba":
            return self._duiba(request)
        if self.kind == "gangganhao":
            return self._gangganhao(request)
        return self._rongliang(request)

    def _duiba(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/drama/page"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {"list": [{"id": 12, "title": "Moon"}], "total": 1},
                },
            )
        if path.endswith("/drama/preview"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [{"serialNo": 1, "lc671EpisodeId": "ep-1"}],
                },
            )
        if path.endswith("/link/create"):
            self.created = True
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "linkNo": "D-1",
                        "minisLink": "https://www.tiktok.com/minis/duiba",
                    },
                },
            )
        if path.endswith("/link/page"):
            if request.url.params.get("linkNo") == "D-1":
                rows = [
                    {
                        "linkNo": "D-1",
                        "dramaId": "12",
                        "miniappId": "77",
                        "defaultEpisode": 1,
                        "cardPointEpisode": 1,
                        "minisLink": "https://www.tiktok.com/minis/duiba",
                    }
                ]
            else:
                rows = []
            return httpx.Response(
                200,
                json={"success": True, "data": {"list": rows, "total": len(rows)}},
            )
        raise AssertionError(request.url)

    def _gangganhao(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/series"):
            assert request.url.params["authorizerAppIds[]"] == "16"
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "list": [
                            {
                                "publishId": 886,
                                "seriesId": 12,
                                "seriesTitle": "Moon",
                                "authorizerAppId": 16,
                            }
                        ],
                        "total": 1,
                    },
                },
            )
        if path.endswith("/series/886"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "publishId": 886,
                        "detail": {
                            "Publish": {
                                "id": 886,
                                "seriesId": 12,
                                "seriesTitle": "Moon",
                            },
                            "Series": {"id": 12, "title": "Moon"},
                        },
                    },
                },
            )
        if path.endswith("/campaign-links"):
            rows = [{"id": 972, "seriesId": 12}] if self.created else []
            return httpx.Response(
                200,
                json={"code": 0, "data": {"list": rows, "total": len(rows)}},
            )
        if path.endswith("/campaign-link") and request.method == "POST":
            self.created = True
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "id": 972,
                        "authorizerAppId": 16,
                        "seriesId": 12,
                        "freeEpisodeCount": 3,
                        "episodeSeq": 1,
                        "paymentTemplateId": 7,
                        "minisLink": "https://www.tiktok.com/minis/ggh",
                    },
                },
            )
        if path.endswith("/campaign-links/972"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "id": 972,
                        "authorizerAppId": 16,
                        "seriesId": 12,
                        "seriesTitle": "Moon",
                        "freeEpisodeCount": 3,
                        "episodeSeq": 1,
                        "paymentTemplateId": 7,
                        "minisLink": "https://www.tiktok.com/minis/ggh",
                    },
                },
            )
        raise AssertionError(request.url)

    def _rongliang(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/compilations/page"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"records": [{"compilationsId": 8453, "originalTitle": "Moon"}], "total": 1},
                },
            )
        if path.endswith("/episodic_dramas"):
            return httpx.Response(
                200,
                json={"code": 0, "data": [{"episodicDramaId": 991, "title": "1"}]},
            )
        if path.endswith("/link/page"):
            if self.created or request.url.params.get("batchId") == "R-1":
                rows = [
                    {
                        "batchId": "R-1",
                        "clientId": 1144,
                        "compilationsId": 8453,
                        "episodicDramaId": 991,
                        "deliverPlatform": 1,
                        "deliveryType": 1,
                        "linkType": 2,
                    }
                ]
            else:
                rows = []
            return httpx.Response(
                200,
                json={"code": 0, "data": {"records": rows, "total": len(rows)}},
            )
        if path.endswith("/link/create"):
            self.created = True
            return httpx.Response(200, json={"code": 0, "data": True})
        if path.endswith("/link/url"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "deepLink": "https://www.tiktok.com/minis/rl?batchId=R-1",
                        "planName": "Moon",
                        "adGroupName": "Moon Ads",
                    },
                },
            )
        raise AssertionError(request.url)


@pytest.fixture
def workflow(isolated_strategy_database, monkeypatch, request):
    db, context, _ = isolated_strategy_database
    kind = request.param
    monkeypatch.setattr(
        settings, "CONNECTION_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    application_id = {"duiba": "77", "gangganhao": "16", "rongliang": "1144"}[kind]
    verification = uuid4()
    channel_config = {
        "verification_token": str(verification),
        **({"delivery_mode": "mixed"} if kind == "gangganhao" else {}),
    }
    with Session(db) as session, session.begin():
        connection = ProviderConnection(
            tenant_id=context.tenant_id,
            kind=kind,
            display_name=f"{kind} fixture",
            status="active",
            credential_version=1,
            verification_token=verification,
            encrypted_credentials=encrypt_credentials(
                tenant_id=context.tenant_id, value={"token": f"{kind}-token"}
            ),
        )
        session.add(connection)
        session.flush()
        session.add(
            ProviderApplication(
                tenant_id=context.tenant_id,
                connection_id=connection.id,
                external_id=application_id,
                name=f"{kind} application",
                channel_config=channel_config,
            )
        )
        session.flush()
        preparation = prepare_links(
            session,
            context=context,
            connection_id=connection.id,
            application_id=application_id,
            lines=["Moon"],
            config=(
                {"episode": 1, "card_point_episode": 1}
                if kind == "duiba"
                else {"free_episode_count": 3, "episode_seq": 1, "payment_template_id": 7}
                if kind == "gangganhao"
                else {"episodic_drama_id": "991"}
            ),
            request_id=uuid4(),
        )
        item_id = session.exec(
            select(LinkPreparationItem.id).where(
                LinkPreparationItem.preparation_id == preparation
            )
        ).one()
        connection_id = connection.id
    return db, context, item_id, connection_id, ProviderRemote(kind)


@pytest.mark.parametrize("workflow", ["duiba", "gangganhao", "rongliang"], indirect=True)
def test_new_provider_workflows_reach_ready_after_create_and_readback(workflow):
    db, context, item_id, _, remote = workflow
    for _ in range(12):
        with Session(db) as session:
            run_link_item(
                session,
                context=context,
                item_id=item_id,
                transport=httpx.MockTransport(remote),
            )
        with Session(db) as session:
            item = session.get(LinkPreparationItem, item_id)
            if item.status == "ready":
                break
            if item.status in {"failed", "config_conflict", "blocked_auth", "result_unknown"}:
                pytest.fail(f"{workflow[0]} stopped at {item.status}: {item.resolved}")
    else:
        pytest.fail("provider workflow did not reach ready")
    assert remote.created
    assert any(path.endswith(("/link/create", "/campaign-link")) for path in remote.calls)
