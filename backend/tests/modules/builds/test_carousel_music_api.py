"""HTTP 范围不接受任意平台账户/连接。"""

import pytest
from sqlmodel import Session

from app.core.errors import DomainError
from app.main import app
from app.modules.builds.carousel_music import browse_music
from tests.modules.accounts.conftest import app_config as app_config
from tests.modules.accounts.conftest import policy as policy
from tests.modules.builds.test_carousel_music import (
    fixed_music_case as fixed_music_case,
)
from tests.modules.builds.test_carousel_music import music_case as music_case
from tests.modules.builds.test_carousel_music import wire as wire


def test_music_routes_registered_and_strict():
    schema = app.openapi()
    prefix = "/api/tenants/{tenant_id}/build-drafts/{draft_id}/music"
    assert prefix + "/search" in schema["paths"]
    assert prefix + "/read" in schema["paths"]
    assert prefix + "/units/{unit_id}" in schema["paths"]
    fields = schema["components"]["schemas"]["MusicRead"]
    assert fields["additionalProperties"] is False
    assert "advertiser_id" not in fields["properties"]


def test_search_pagination_empty_and_read_refresh(music_case, wire, redis_client):
    from tests.integrations.tiktok.test_music_adapters import music_row

    engine, scope = music_case
    kwargs = {
        "database_engine": engine,
        "redis_client": redis_client,
        "context": scope.context,
        "draft_id": scope.draft_id,
    }
    wire["rows"] = [music_row(str(i)) for i in range(100)]
    wire["total"] = 101
    first = browse_music(**kwargs, keyword="moon")
    assert len(first["items"]) == 100 and first["next_cursor"]
    wire["rows"] = [music_row("last")]
    second = browse_music(**kwargs, keyword="moon", cursor=first["next_cursor"])
    assert len(second["items"]) == 1 and second["next_cursor"] is None
    wire["total"] = None
    wire["rows"] = []
    assert browse_music(**kwargs, keyword="missing")["items"] == []
    wire["rows"] = [
        music_row("one", url="https://music.example.test/new?signature=fresh")
    ]
    result = browse_music(**kwargs, music_ids=["one"])
    assert result["items"][0]["preview_url"].endswith("signature=fresh")
    assert result["items"][0]["duration"] == 0
    assert "route" not in result and "access_token" not in str(result)


def test_search_timeout_is_not_absent(music_case, wire, redis_client):
    from urllib3.exceptions import ReadTimeoutError

    engine, scope = music_case
    wire["error"] = ReadTimeoutError(None, None, "synthetic timeout")
    with pytest.raises(DomainError) as error:
        browse_music(
            database_engine=engine,
            redis_client=redis_client,
            context=scope.context,
            draft_id=scope.draft_id,
            keyword="moon",
        )
    assert error.value.code == "music_timeout"


@pytest.mark.parametrize("revocation", ["binding", "membership", "account"])
def test_read_returns_no_url_after_return_time_revocation(
    music_case, wire, redis_client, revocation
):
    from app.modules.accounts.connection_models import BCConnectionBinding
    from app.modules.accounts.models import BCAccountAccess
    from app.modules.tenants.models import TenantMembership

    engine, scope = music_case

    def revoke():
        with Session(engine) as session, session.begin():
            if revocation == "binding":
                session.get(
                    BCConnectionBinding,
                    (scope.tenant_id, scope.bc_id, scope.route.connection_id),
                ).revision += 1
            elif revocation == "membership":
                session.get(
                    TenantMembership, (scope.tenant_id, scope.actor_id)
                ).active = False
            else:
                session.get(
                    BCAccountAccess,
                    (
                        scope.tenant_id,
                        scope.bc_id,
                        "account-A",
                        scope.route.connection_id,
                    ),
                ).active = False

    wire["hook"] = revoke
    with pytest.raises(DomainError):
        browse_music(
            database_engine=engine,
            redis_client=redis_client,
            context=scope.context,
            draft_id=scope.draft_id,
            music_ids=["recommended"],
        )
    assert len(wire["calls"]) == 1


def test_http_scope_strict_inputs_summary_and_timeout(music_case, wire, monkeypatch):
    from uuid import uuid4

    from fastapi.testclient import TestClient
    from urllib3.exceptions import ReadTimeoutError

    from app.api.deps import get_current_user, get_db
    from app.core.config import settings
    from app.main import app
    from app.models import User

    engine, scope = music_case
    monkeypatch.setattr(settings, "REDIS_URL", "redis://localhost:6379/14")

    def database():
        with Session(engine) as session:
            yield session

    def user():
        with Session(engine) as session:
            return session.get(User, scope.actor_id)

    original = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = user
    url = f"/api/tenants/{scope.tenant_id}/build-drafts/{scope.draft_id}/music"
    try:
        with TestClient(app) as client:
            assert (
                client.post(
                    url + "/read",
                    json={"music_ids": ["recommended"], "advertiser_id": "foreign"},
                ).status_code
                == 422
            )
            result = client.get(url + "/search", params={"keyword": "moon"})
            assert result.status_code == 200, result.text
            assert result.json()["items"][0]["music_id"] == "recommended"
            summary = client.get(url)
            assert summary.status_code == 200 and summary.json()["jobs"] == {
                "PENDING": 2
            }
            assert client.get(
                url.replace(str(scope.tenant_id), str(uuid4())) + "/search",
                params={"keyword": "moon"},
            ).status_code in {403, 404}
            assert (
                client.get(
                    url.replace(str(scope.draft_id), str(uuid4())) + "/search",
                    params={"keyword": "moon"},
                ).status_code
                == 404
            )
            wire["error"] = ReadTimeoutError(None, None, "synthetic")
            response = client.get(url + "/search", params={"keyword": "moon"})
            assert (
                response.status_code == 504
                and response.json()["code"] == "music_timeout"
            )
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(original)


def test_other_bc_cannot_borrow_recorded_seed(music_case, wire, redis_client):
    from uuid import uuid4

    from app.modules.accounts.connection_models import (
        BCConnectionBinding,
        BCDefaultRoute,
    )
    from app.modules.accounts.models import TenantBC
    from app.modules.builds.models import BuildDraft

    engine, scope = music_case
    with Session(engine) as session, session.begin():
        session.add(TenantBC(tenant_id=scope.tenant_id, bc_id="other-bc"))
        session.flush()
        session.add(
            BCConnectionBinding(
                tenant_id=scope.tenant_id,
                bc_id="other-bc",
                connection_id=scope.route.connection_id,
                kind="OFFICIAL_API",
            )
        )
        session.flush()
        session.add(
            BCDefaultRoute(
                tenant_id=scope.tenant_id,
                bc_id="other-bc",
                connection_id=scope.route.connection_id,
            )
        )
        source = session.get(BuildDraft, scope.draft_id)
        other = BuildDraft(
            **{
                **source.model_dump(),
                "id": uuid4(),
                "bc_id": "other-bc",
                "request_id": uuid4(),
                "active_image_generation": 0,
            }
        )
        session.add(other)
        other_id = other.id
    with pytest.raises(DomainError) as error:
        browse_music(
            database_engine=engine,
            redis_client=redis_client,
            context=scope.context,
            draft_id=other_id,
            keyword="moon",
        )
    assert error.value.code == "image_source_unavailable" and not wire["calls"]


def test_http_fixed_multi_recommend_rejected_without_job(fixed_music_case, wire):
    from uuid import uuid4

    from fastapi.testclient import TestClient

    from app.api.deps import get_current_user, get_db
    from app.models import User
    from app.modules.builds.image_composition_models import DraftCreativeUnit
    from app.modules.builds.models import BuildDraft
    from tests.modules.builds.test_carousel_music import job_ids

    engine, scope, unit_id = fixed_music_case

    def database():
        with Session(engine) as session:
            yield session

    def user():
        with Session(engine) as session:
            return session.get(User, scope.actor_id)

    with Session(engine) as session:
        draft = session.get(BuildDraft, scope.draft_id)
        generation, revision = draft.active_image_generation, draft.revision
        unit = session.get(
            DraftCreativeUnit, (scope.tenant_id, scope.draft_id, generation, unit_id)
        )
        before = unit.model_dump()
        body = {
            "request_id": str(uuid4()),
            "expected_content_revision": unit.revision,
            "expected_music_revision": unit.music_revision,
            "action": "RECOMMEND",
        }
    old_jobs = job_ids(engine)
    original = dict(app.dependency_overrides)
    app.dependency_overrides[get_db], app.dependency_overrides[get_current_user] = (
        database,
        user,
    )
    try:
        with TestClient(app) as client:
            response = client.post(
                f"/api/tenants/{scope.tenant_id}/build-drafts/{scope.draft_id}/music/units/{unit_id}",
                json=body,
            )
            assert (
                response.status_code == 422
                and response.json()["code"] == "music_request_invalid"
            )
        with Session(engine) as session:
            assert session.get(BuildDraft, scope.draft_id).revision == revision
            assert (
                session.get(
                    DraftCreativeUnit,
                    (scope.tenant_id, scope.draft_id, generation, unit_id),
                ).model_dump()
                == before
            )
        assert job_ids(engine) == old_jobs and not wire["calls"]
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(original)
