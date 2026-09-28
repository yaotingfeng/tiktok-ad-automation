from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlmodel import select

from app.core.errors import DomainError
from app.modules.accounts.routing import freeze_route
from app.modules.builds.models import DraftAccount
from app.modules.builds.scene_job_models import SceneJob
from tests.modules.builds.test_drafts import account


@pytest.fixture
def directory_env(session, context, intent):
    from app.modules.builds.drafts import create_draft

    account(session, context, "account-A")
    account(session, context, "account-B")
    draft_id = create_draft(session, context=context, **intent)
    route = freeze_route(session, context=context, bc_id=intent["bc_id"])
    jobs = []
    for advertiser, countries in [
        ("account-A", ["US", "CA"]),
        ("account-B", ["US", "GB"]),
    ]:
        session.add(
            DraftAccount(
                tenant_id=context.tenant_id,
                draft_id=draft_id,
                advertiser_id=advertiser,
                bc_id=route.bc_id,
                connection_id=route.connection_id,
                currency="USD",
                timezone="UTC",
                first_line=1,
            )
        )
        job = SceneJob(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            bc_id=route.bc_id,
            advertiser_id=advertiser,
            connection_id=route.connection_id,
            credential_revision=0,
            frozen_route=route.model_dump(mode="json"),
            minis_id="mini",
            scope_basis=uuid4().hex,
            status="COMPLETE",
            first_observed_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            facts={
                "minis": {
                    "matches": [
                        {
                            "minis_id": "mini",
                            "status": "ACTIVE",
                            "type": "MINI_SERIES",
                            "regions": countries,
                        }
                    ]
                },
                "regions": {
                    "locations": [
                        {"region_code": code, "location_id": str(i + 100)}
                        for i, code in enumerate(countries)
                    ]
                },
            },
        )
        session.add(job)
        jobs.append(job)
    session.flush()
    return draft_id, route, jobs


def test_common_countries_and_reference_union_are_scoped(
    session, context, directory_env
):
    from app.modules.builds.targeting_directory import region_directory

    draft_id, route, _ = directory_env
    result = region_directory(
        session, context=context, route=route, draft_id=draft_id, minis_id="mini"
    )
    assert result.region_codes == ["US"]
    assert result.account_count == result.verified_account_count == 2
    assert result.state == "READY"
    reference = region_directory(session, context=context, route=route)
    assert reference.region_codes == ["CA", "GB", "US"]


@pytest.mark.parametrize("change", ["expired", "revoked", "old_route", "pending"])
def test_unknown_account_is_not_dropped_from_intersection(
    session, context, directory_env, change
):
    from app.modules.accounts.models import BCAccountAccess
    from app.modules.builds.targeting_directory import region_directory

    draft_id, route, jobs = directory_env
    job = jobs[1]
    if change == "expired":
        job.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    if change == "old_route":
        job = SceneJob(
            **{
                **job.model_dump(),
                "id": uuid4(),
                "scope_basis": uuid4().hex,
                "created_at": datetime.now(UTC),
                "frozen_route": {
                    **job.frozen_route,
                    "authorization_revision": route.authorization_revision + 1,
                },
            }
        )
    if change == "pending":
        job.status = "PENDING"
    if change == "revoked":
        grant = session.get(
            BCAccountAccess,
            (context.tenant_id, route.bc_id, "account-B", route.connection_id),
        )
        grant.authorized = False
        session.add(grant)
    session.add(job)
    session.flush()
    result = region_directory(
        session, context=context, route=route, draft_id=draft_id, minis_id="mini"
    )
    assert result.account_count == 2
    assert result.verified_account_count == 1
    assert result.region_codes == []
    assert result.state != "READY"


def test_override_is_idempotent_and_clear_restores_strategy(
    session, context, directory_env
):
    from app.modules.builds.models import BuildDraft
    from app.modules.builds.targeting_schemas import TargetingChange
    from app.modules.builds.targeting_service import effective_targeting, save_targeting

    draft_id, _, _ = directory_env
    draft = session.get(BuildDraft, draft_id)
    draft.status = "READY"
    session.add(draft)
    session.flush()
    body = TargetingChange(
        request_id=uuid4(),
        expected_revision=draft.revision,
        targeting_override={
            "region_mode": "SELECTED",
            "region_codes": ["US"],
            "languages": ["en"],
        },
    )
    revision = save_targeting(session, context=context, draft_id=draft_id, body=body)
    assert revision == 2
    assert save_targeting(session, context=context, draft_id=draft_id, body=body) == 2
    assert effective_targeting(session, context, draft).languages == ("en",)
    assert (
        len(
            session.exec(
                select(DraftAccount).where(DraftAccount.draft_id == draft_id)
            ).all()
        )
        == 2
    )
    draft.status = "READY"
    session.add(draft)
    session.flush()
    save_targeting(
        session,
        context=context,
        draft_id=draft_id,
        body=TargetingChange(
            request_id=uuid4(), expected_revision=2, targeting_override=None
        ),
    )
    assert draft.targeting_override is None
    assert effective_targeting(session, context, draft).region_mode == "ALL_AVAILABLE"
    with pytest.raises(DomainError, match="草稿"):
        save_targeting(
            session,
            context=context,
            draft_id=draft_id,
            body=TargetingChange(
                request_id=uuid4(), expected_revision=1, targeting_override=None
            ),
        )


@pytest.mark.parametrize(
    "change", ["missing_check", "future_check", "authorization_missing", "read_denied"]
)
def test_missing_authorization_evidence_never_becomes_ready(
    session, context, directory_env, change
):
    from app.modules.accounts.connection_models import ConnectionAuthorization
    from app.modules.accounts.models import BCAccountAccess
    from app.modules.builds.targeting_directory import region_directory

    draft_id, route, _ = directory_env
    if change.endswith("check"):
        grant = session.get(
            BCAccountAccess,
            (context.tenant_id, route.bc_id, "account-B", route.connection_id),
        )
        grant.checked_at = (
            None if change == "missing_check" else datetime.now(UTC) + timedelta(days=1)
        )
        session.add(grant)
    else:
        authorization = session.exec(
            select(ConnectionAuthorization).where(
                ConnectionAuthorization.connection_id == route.connection_id
            )
        ).one()
        if change == "authorization_missing":
            session.delete(authorization)
        else:
            authorization.permission_summary = {
                **authorization.permission_summary,
                "read_authorized": False,
            }
            session.add(authorization)
    session.flush()
    result = region_directory(
        session, context=context, route=route, draft_id=draft_id, minis_id="mini"
    )
    assert result.account_count == 2
    assert result.region_codes == []
    assert result.state == "PENDING"


def test_verified_empty_country_intersection_is_unavailable(
    session, context, directory_env
):
    from app.modules.builds.targeting_directory import region_directory

    draft_id, route, jobs = directory_env
    job = jobs[1]
    job.facts = {**job.facts, "regions": {"locations": []}}
    session.add(job)
    session.flush()
    result = region_directory(
        session, context=context, route=route, draft_id=draft_id, minis_id="mini"
    )
    assert result.verified_account_count == result.account_count == 2
    assert result.region_codes == []
    assert result.state == "UNAVAILABLE"


def test_other_bc_scene_never_expands_reference_or_batch_countries(
    session, context, directory_env
):
    from app.modules.accounts.models import TenantBC
    from app.modules.builds.targeting_directory import region_directory

    draft_id, route, jobs = directory_env
    session.add(TenantBC(tenant_id=context.tenant_id, bc_id="other-bc"))
    session.flush()
    job = SceneJob(
        **{
            **jobs[0].model_dump(),
            "id": uuid4(),
            "bc_id": "other-bc",
            "scope_basis": uuid4().hex,
            "frozen_route": {**jobs[0].frozen_route, "bc_id": "other-bc"},
            "facts": {
                "minis": {
                    "matches": [
                        {
                            "minis_id": "mini",
                            "status": "ACTIVE",
                            "type": "MINI_SERIES",
                            "regions": ["JP"],
                        }
                    ]
                },
                "regions": {"locations": [{"region_code": "JP", "location_id": "999"}]},
            },
        }
    )
    session.add(job)
    session.flush()
    assert region_directory(session, context=context, route=route).region_codes == [
        "CA",
        "GB",
        "US",
    ]
    assert region_directory(
        session, context=context, route=route, draft_id=draft_id, minis_id="mini"
    ).region_codes == ["US"]
