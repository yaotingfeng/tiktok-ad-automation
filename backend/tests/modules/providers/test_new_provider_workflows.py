"""Durable end-to-end preparation checks for the three newly wired providers."""

# ruff: noqa: F811

import json
from dataclasses import dataclass, field
from typing import Any, NamedTuple
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlmodel import Session, select

from app.core.config import settings
from app.core.credentials import encrypt_credentials
from app.modules.providers.link_steps import TERMINAL, run_link_item
from app.modules.providers.models import (
    LinkPreparationItem,
    ProviderApplication,
    ProviderConnection,
)
from app.modules.providers.service import choose_drama_candidate, prepare_links
from tests.modules.strategies.test_concurrency import (  # noqa: F401
    isolated_strategy_database,
)

DUIBA_DRAMA = {"id": 12, "title": "Moon", "freeEpisodeCount": 8, "episodeCount": 10}
GGH_DRAMA = {
    "publishId": 886,
    "seriesId": 12,
    "seriesTitle": "Moon",
    "authorizerAppId": 16,
    "freeEpisodeCount": 3,
    "episodeCount": 10,
}
RL_DRAMA = {"compilationsId": 8453, "originalTitle": "Moon"}
# 同名第二部剧：用于验证多候选时人工选择后仍进入版权方详情阶段。
SECOND_DRAMA = {
    "duiba": {**DUIBA_DRAMA, "id": 13},
    "gangganhao": {**GGH_DRAMA, "publishId": 887, "seriesId": 13},
    "rongliang": {**RL_DRAMA, "compilationsId": 8454},
}


@dataclass
class ProviderRemote:
    kind: str
    dramas: list[dict] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    bodies: list[dict] = field(default_factory=list)
    created: bool = False

    def __post_init__(self) -> None:
        if not self.dramas:
            self.dramas = [
                {"duiba": DUIBA_DRAMA, "gangganhao": GGH_DRAMA, "rongliang": RL_DRAMA}[
                    self.kind
                ]
            ]

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
                    "data": {"list": self.dramas, "total": len(self.dramas)},
                },
            )
        if path.endswith("/drama/preview"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [
                        {"serialNo": number, "lc671EpisodeId": f"ep-{number}"}
                        for number in range(1, 11)
                    ],
                },
            )
        if path.endswith("/link/create"):
            self.created = True
            self.bodies.append(json.loads(request.content))
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
            rows = []
            if request.url.params.get("linkNo") == "D-1" and self.bodies:
                body = self.bodies[-1]
                rows = [
                    {
                        "linkNo": "D-1",
                        "dramaId": body["dramaId"],
                        "miniappId": str(body["miniappId"]),
                        "defaultEpisode": body["defaultEpisode"],
                        "cardPointEpisode": body["cardPointEpisode"],
                        "minisLink": "https://www.tiktok.com/minis/duiba",
                    }
                ]
            return httpx.Response(
                200,
                json={"success": True, "data": {"list": rows, "total": len(rows)}},
            )
        raise AssertionError(request.url)

    def _ggh_link(self) -> dict:
        body = self.bodies[-1]
        return {
            "id": 972,
            "authorizerAppId": body["authorizerAppId"],
            "seriesId": body["seriesId"],
            "seriesTitle": body["seriesTitle"],
            "freeEpisodeCount": body["freeEpisodeCount"],
            "episodeSeq": body["episodeSeq"],
            "paymentTemplateId": body.get("paymentTemplateId"),
            "minisLink": "https://www.tiktok.com/minis/ggh",
        }

    def _gangganhao(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/series"):
            assert request.url.params["authorizerAppIds[]"] == "16"
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"list": self.dramas, "total": len(self.dramas)},
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
            self.bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"code": 0, "data": self._ggh_link()})
        if path.endswith("/campaign-links/972"):
            return httpx.Response(200, json={"code": 0, "data": self._ggh_link()})
        raise AssertionError(request.url)

    def _rongliang(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/compilations/page"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"records": self.dramas, "total": len(self.dramas)},
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


DEFAULT_CONFIG = {
    "duiba": {"episode": 1, "card_point_episode": 1},
    "gangganhao": {"free_episode_count": 3, "episode_seq": 1, "payment_template_id": 7},
    "rongliang": {},
}


class Workflow(NamedTuple):
    db: Any
    context: Any
    item_id: Any
    connection_id: Any
    remote: ProviderRemote


@pytest.fixture
def make_workflow(isolated_strategy_database, monkeypatch):
    db, context, _ = isolated_strategy_database
    monkeypatch.setattr(
        settings, "CONNECTION_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )

    def build(kind, config=None, dramas=None):
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
                config=DEFAULT_CONFIG[kind] if config is None else config,
                request_id=uuid4(),
            )
            item_id = session.exec(
                select(LinkPreparationItem.id).where(
                    LinkPreparationItem.preparation_id == preparation
                )
            ).one()
            connection_id = connection.id
        return Workflow(
            db, context, item_id, connection_id, ProviderRemote(kind, dramas=dramas or [])
        )

    return build


@pytest.fixture
def workflow(make_workflow, request):
    return make_workflow(request.param)


def finish(workflow: Workflow) -> LinkPreparationItem:
    """推进到终态（含待选择候选），返回该行。"""

    for _ in range(12):
        with Session(workflow.db) as session:
            run_link_item(
                session,
                context=workflow.context,
                item_id=workflow.item_id,
                transport=httpx.MockTransport(workflow.remote),
            )
        with Session(workflow.db) as session:
            item = session.get(LinkPreparationItem, workflow.item_id)
            if item.status in TERMINAL | {"result_unknown"}:
                return item
    pytest.fail("provider workflow did not finish")


def created_body(workflow: Workflow, *, allow_missing: bool = False) -> dict | None:
    if allow_missing and not workflow.remote.bodies:
        return None
    return workflow.remote.bodies[-1]


def choose_first_candidate(workflow: Workflow) -> None:
    with Session(workflow.db) as session, session.begin():
        item = session.get(LinkPreparationItem, workflow.item_id)
        choose_drama_candidate(
            session,
            context=workflow.context,
            input_id=workflow.item_id,
            external_drama_id=item.resolved["_work"]["candidates"][0][
                "external_drama_id"
            ],
        )


@pytest.mark.parametrize("workflow", ["duiba", "gangganhao", "rongliang"], indirect=True)
def test_new_provider_workflows_reach_ready_after_create_and_readback(workflow):
    item = finish(workflow)
    assert item.status == "ready", item.resolved
    assert workflow.remote.created
    assert any(
        path.endswith(("/link/create", "/campaign-link")) for path in workflow.remote.calls
    )


def test_duiba_card_point_defaults_to_each_drama_free_count(make_workflow):
    workflow = make_workflow("duiba", {})
    item = finish(workflow)
    assert item.status == "ready", item.resolved
    body = created_body(workflow)
    assert body["defaultEpisode"] == 1
    assert body["cardPointEpisode"] == 8  # 剧单 freeEpisodeCount
    # 剧事实只保留在本次 work，不写入剧目表。
    assert item.resolved["_work"]["drama"]["free_episode_count"] == 8


def test_duiba_card_point_falls_back_to_start_episode_without_drama_free_count(
    make_workflow,
):
    drama = {key: value for key, value in DUIBA_DRAMA.items() if key != "freeEpisodeCount"}
    workflow = make_workflow("duiba", {"episode": 2}, dramas=[drama])
    assert finish(workflow).status == "ready"
    assert created_body(workflow)["cardPointEpisode"] == 2


def test_duiba_explicit_card_point_override_wins(make_workflow):
    workflow = make_workflow("duiba", {"card_point_episode": 5})
    assert finish(workflow).status == "ready"
    assert created_body(workflow)["cardPointEpisode"] == 5


@pytest.mark.parametrize("free, expected", [(6, 6), (None, 3)])
def test_gangganhao_free_count_defaults_to_drama_then_three(make_workflow, free, expected):
    drama = {key: value for key, value in GGH_DRAMA.items() if key != "freeEpisodeCount"}
    if free is not None:
        drama["freeEpisodeCount"] = free
    workflow = make_workflow("gangganhao", {"payment_template_id": 7}, dramas=[drama])
    item = finish(workflow)
    assert item.status == "ready", item.resolved
    body = created_body(workflow)
    assert body["freeEpisodeCount"] == expected
    assert body["episodeSeq"] == 1


def test_gangganhao_sequence_beyond_drama_free_count_is_config_conflict(make_workflow):
    workflow = make_workflow(
        "gangganhao", {"episode_seq": 5, "payment_template_id": 7}
    )  # 剧免费集数 3
    item = finish(workflow)
    assert item.status == "config_conflict"
    assert item.resolved["error_code"] == "link_episode_out_of_range"
    assert item.resolved["error_message"]
    assert not created_body(workflow, allow_missing=True)


def test_gangganhao_free_count_beyond_episode_total_is_config_conflict(make_workflow):
    workflow = make_workflow(
        "gangganhao", {"free_episode_count": 11, "payment_template_id": 7}
    )  # 剧总集数 10
    item = finish(workflow)
    assert item.status == "config_conflict"
    assert item.resolved["error_code"] == "link_episode_out_of_range"
    assert not created_body(workflow, allow_missing=True)


@pytest.mark.parametrize("kind", ["duiba", "gangganhao", "rongliang"])
def test_selected_candidate_enters_provider_detail_stage(make_workflow, kind):
    first = {"duiba": DUIBA_DRAMA, "gangganhao": GGH_DRAMA, "rongliang": RL_DRAMA}[kind]
    workflow = make_workflow(
        kind, {} if kind != "gangganhao" else {"payment_template_id": 7},
        dramas=[first, SECOND_DRAMA[kind]],
    )
    item = finish(workflow)
    assert item.status == "needs_resolution"
    choose_first_candidate(workflow)
    item = finish(workflow)
    assert item.status == "ready", item.resolved
