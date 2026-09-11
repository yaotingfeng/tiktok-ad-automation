"""Public-document fixtures; real PostgreSQL/Redis, official urllib3 wire only."""

import json
from uuid import uuid4

import pytest
from sqlmodel import Session

from app.core.credentials import encrypt_credentials
from app.core.db import engine
from app.modules.accounts.models import BCAccountAccess, TikTokConnection
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire
from tests.modules.providers.test_tenant_repository import promotion, seed_provider


@pytest.fixture
def scene_env(source_env, monkeypatch):
    from app.modules.builds import scene

    monkeypatch.setattr(scene, "_require_bounded_worker", lambda: None)
    with Session(engine) as session, session.begin():
        provider, app, drama = seed_provider(session, source_env["context"])
        provider.status = "active"
        provider.verification_token = uuid4()
        app.channel_config = {"verification_token": str(provider.verification_token)}
        app.tiktok_minis_id = "fixture-minis"
        link = promotion(source_env["context"], provider, drama)
        session.add(link)
        session.flush()
        conn = session.get(TikTokConnection, source_env["connection_id"])
        conn.credential_ciphertext = encrypt_credentials(
            tenant_id=conn.tenant_id,
            value={"access_token": "offline-token-secret", "scope": "[2,6]"},
        )
        grant = session.get(
            BCAccountAccess,
            (conn.tenant_id, source_env["bc_id"], "actual-account", conn.id),
        )
        grant.permission_state, grant.can_build, grant.can_upload = (
            "UNKNOWN",
            False,
            False,
        )
        source_env["link_id"] = link.id
    return source_env


def read(env):
    from app.modules.builds.scene import read_scene_context

    with Session(engine) as session:
        return read_scene_context(
            session,
            context=env["context"],
            bc_id=env["bc_id"],
            advertiser_id="actual-account",
            link_id=env["link_id"],
        )


def refresh(env, redis_client, resource, previous_evidence_id=None):
    from app.modules.builds.scene import refresh_scene_context

    return refresh_scene_context(
        database_engine=engine,
        redis_client=redis_client,
        context=env["context"],
        bc_id=env["bc_id"],
        advertiser_id="actual-account",
        link_id=env["link_id"],
        resource=resource,
        previous_evidence_id=previous_evidence_id,
    )


def page(items, *, key="list", number=1, total=1):
    return {
        key: items,
        "page_info": {
            "page": number,
            "page_size": 50,
            "total_page": total,
            "total_number": len(items) if total == 1 else 51,
        },
    }


def test_preview_reads_missing_evidence_without_network_or_mutation(scene_env, wire):
    result = read(scene_env)
    assert not result.supported and "scene_evidence_missing" in result.reason_codes
    assert result.copy_length_limit == 100
    assert result.field_constraints["platform_copy_length"] is None
    assert not wire[0]
    with Session(engine) as session:
        grant = session.get(
            BCAccountAccess,
            (
                scene_env["context"].tenant_id,
                scene_env["bc_id"],
                "actual-account",
                scene_env["connection_id"],
            ),
        )
        assert grant.permission_state == "UNKNOWN" and not grant.can_build
    with pytest.raises((TypeError, AttributeError)):
        result.campaign_fields["objective_type"] = "APP_INSTALL"
    assert json.loads(json.dumps(result.to_snapshot()))["supported"] is False


def test_current_token_role_plus_scope_establishes_capability_only_on_refresh(
    scene_env, wire, redis_client
):
    wire[1].append(
        page(
            [
                {
                    "asset_id": "actual-account",
                    "asset_type": "ADVERTISER",
                    "advertiser_role": "OPERATOR",
                }
            ]
        )
    )
    receipt = refresh(scene_env, redis_client, "account_roles")
    assert receipt.complete
    method, url, kwargs = wire[0][0]
    assert method == "GET" and url.endswith("/bc/asset/get/")
    fields = dict(kwargs["fields"])
    assert fields["bc_id"] == scene_env["bc_id"] and "filtering" not in fields
    with Session(engine) as session:
        grant = session.get(
            BCAccountAccess,
            (
                scene_env["context"].tenant_id,
                scene_env["bc_id"],
                "actual-account",
                scene_env["connection_id"],
            ),
        )
        assert (
            grant.permission_state == "VERIFIED"
            and grant.can_build
            and grant.can_upload
        )
    result = read(scene_env)
    assert not result.supported  # Other required scene facts are still missing.
    assert "account_scope_unverified" in result.reason_codes
    assert "field_limits_unverified" not in result.reason_codes
    assert receipt.evidence_id in result.evidence_ids


def test_minis_pagination_requires_complete_chain_and_exact_application(
    scene_env, wire, redis_client
):
    wire[1].append(
        page(
            [
                {
                    "minis_id": f"other-{i}",
                    "minis_status": "ACTIVE",
                    "minis_type": "MINI_SERIES",
                    "region_codes": ["US"],
                }
                for i in range(50)
            ],
            total=2,
        )
    )
    first = refresh(scene_env, redis_client, "minis")
    assert not first.complete and first.next_page == 2
    wire[1].append(
        page(
            [
                {
                    "minis_id": "fixture-minis",
                    "minis_status": "ACTIVE",
                    "minis_type": "MINI_SERIES",
                    "region_codes": ["US"],
                }
            ],
            number=2,
            total=2,
        )
    )
    second = refresh(scene_env, redis_client, "minis", first.evidence_id)
    assert second.complete and second.next_page is None
    assert read(scene_env).adgroup_fields["minis_id"] == "fixture-minis"
    assert dict(wire[0][1][2]["fields"]) == {
        "advertiser_id": "actual-account",
        "page": 2,
        "page_size": 50,
    }


def test_scene_tables_enforce_tenant_relationships(scene_env):
    from sqlalchemy.exc import IntegrityError

    from app.modules.builds.scene_models import SceneReadState
    from app.modules.tenants.models import Tenant

    with Session(engine) as session:
        foreign = Tenant(name="Foreign scene fixture")
        session.add(foreign)
        session.flush()
        row = SceneReadState(
            tenant_id=foreign.id,
            bc_id=scene_env["bc_id"],
            advertiser_id="actual-account",
            link_id=scene_env["link_id"],
            connection_id=scene_env["connection_id"],
            resource="minis",
            basis_digest="a" * 64,
        )
        session.add(row)
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()


def role_page(role="OPERATOR"):
    return page(
        [
            {
                "asset_id": "actual-account",
                "asset_type": "ADVERTISER",
                "advertiser_role": role,
            }
        ]
    )


def identity(bc_id, suffix="1"):
    return {
        "identity_id": f"identity-{suffix}",
        "identity_type": "BC_AUTH_TT",
        "identity_authorized_bc_id": bc_id,
        "available_status": "AVAILABLE",
        "can_push_video": True,
        "is_gpppa": False,
    }


@pytest.mark.parametrize(
    "private,role,expected",
    [
        ({"access_token": "legacy-token"}, "OPERATOR", ("UNKNOWN", False, False)),
        (
            {"access_token": "token", "scope": "[2,6]"},
            "ANALYST",
            ("VERIFIED", False, False),
        ),
        ({"access_token": "token", "scope": "[6]"}, "ADMIN", ("VERIFIED", False, True)),
        (
            {"access_token": "token", "scope": "[2]"},
            "OPERATOR",
            ("VERIFIED", True, False),
        ),
        (
            {"access_token": "token", "scope": "[true]"},
            "ADMIN",
            ("UNKNOWN", False, False),
        ),
    ],
)
def test_scope_and_actual_token_role_are_independent(
    scene_env, wire, redis_client, private, role, expected
):
    with Session(engine) as session, session.begin():
        conn = session.get(TikTokConnection, scene_env["connection_id"])
        conn.credential_ciphertext = encrypt_credentials(
            tenant_id=conn.tenant_id, value=private
        )
    wire[1].append(role_page(role))
    assert refresh(scene_env, redis_client, "account_roles").complete
    with Session(engine) as session:
        conn = session.get(TikTokConnection, scene_env["connection_id"])
        grant = session.get(
            BCAccountAccess,
            (conn.tenant_id, scene_env["bc_id"], "actual-account", conn.id),
        )
        assert (grant.permission_state, grant.can_build, grant.can_upload) == expected
        assert conn.status == "ACTIVE"  # Old receipt still permits reads.
    assert not read(scene_env).supported


def test_legacy_scene_remains_diagnostic_without_shared_proof_and_targeting(
    scene_env, wire, redis_client
):
    wire[1].extend(
        [
            role_page(),
            page([identity(scene_env["bc_id"])], key="identity_list"),
            page(
                [
                    {
                        "minis_id": "fixture-minis",
                        "minis_status": "ACTIVE",
                        "minis_type": "MINI_SERIES",
                        "region_codes": ["US", "CA"],
                    }
                ]
            ),
            {
                "recommend_assets": [
                    {
                        "asset_ids": ["cta-2", "cta-1"],
                        "asset_content": "Watch now",
                        "unneeded_description": "private text excluded",
                    }
                ]
            },
            {"vo_min_roas": "QUALIFIED", "vo_status": "QUALIFIED"},
        ]
    )
    for resource in ("account_roles", "identity", "minis", "cta", "vbo"):
        assert refresh(scene_env, redis_client, resource).complete
    result = read(scene_env)
    assert set(result.reason_codes) == {
        "account_build_unverified",
        "account_scope_unverified",
        "scene_evidence_missing",
        "scene_targeting_unavailable",
    }
    assert result.cta_fields["asset_ids"] == ("cta-1", "cta-2")
    assert result.cta_fields["recommend_assets"] == (
        {"asset_ids": ("cta-1", "cta-2"), "asset_content": "Watch now"},
    )
    assert result.creative_fields["creative_info"]["identity_id"] == "identity-1"
    assert result.field_constraints["allowed_region_codes"] == ("CA", "US")
    assert len(result.evidence_ids) == 5
    identity_query = dict(wire[0][1][2]["fields"])
    assert identity_query["identity_type"] == "BC_AUTH_TT"
    assert identity_query["identity_authorized_bc_id"] == scene_env["bc_id"]
    cta_query = dict(wire[0][3][2]["fields"])
    assert (
        cta_query["promotion_type"] == "MINI_APP" and cta_query["new_version"] == "true"
    )
    vbo_query = dict(wire[0][4][2]["fields"])
    assert vbo_query["campaign_automation_type"] == "UPGRADED_SMART_PLUS"
    assert (
        vbo_query["app_promotion_type"] == "MINIS"
        and vbo_query["budget_optimize_on"] == "true"
    )
    assert all(call[0] == "GET" for call in wire[0])
    assert all("app_id" not in dict(call[2]["fields"]) for call in wire[0])
    assert "private text" not in json.dumps(result.to_snapshot())


@pytest.mark.parametrize(
    "variant,reason",
    [
        ("ambiguous", "identity_selection_required"),
        ("no_push", "identity_unavailable"),
        ("political", "identity_unavailable"),
        ("wrong_bc", "scene_response_unverified"),
    ],
)
def test_identity_never_uses_first_or_unowned_asset(
    scene_env, wire, redis_client, variant, reason
):
    values = [identity(scene_env["bc_id"])]
    if variant == "ambiguous":
        values.append(identity(scene_env["bc_id"], "2"))
    elif variant == "no_push":
        values[0]["can_push_video"] = False
    elif variant == "political":
        values[0]["is_gpppa"] = True
    else:
        values[0]["identity_authorized_bc_id"] = "foreign-bc"
    wire[1].append(page(values, key="identity_list"))
    refresh(scene_env, redis_client, "identity")
    result = read(scene_env)
    assert reason in result.reason_codes
    assert not result.creative_fields


def test_expired_and_rotated_facts_cannot_be_used(scene_env, wire, redis_client):
    from datetime import UTC, datetime, timedelta

    from sqlmodel import select

    from app.modules.builds.scene_models import SceneReadState

    wire[1].append(role_page())
    refresh(scene_env, redis_client, "account_roles")
    original = read(scene_env)
    with Session(engine) as session, session.begin():
        state = session.exec(select(SceneReadState)).one()
        state.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert "scene_evidence_expired" in read(scene_env).reason_codes
    with Session(engine) as session, session.begin():
        connection = session.get(TikTokConnection, scene_env["connection_id"])
        connection.credential_revision += 1
    changed = read(scene_env)
    assert (
        not changed.evidence_ids
        and changed.capability_revision != original.capability_revision
    )


def test_preview_reloads_local_build_permission_without_mutating(
    scene_env, wire, redis_client
):
    from app.modules.accounts.models import AdvertiserAccount

    wire[1].append(role_page())
    refresh(scene_env, redis_client, "account_roles")
    with Session(engine) as session, session.begin():
        account = session.get(
            AdvertiserAccount, (scene_env["context"].tenant_id, "actual-account")
        )
        account.remote_status = "STATUS_DISABLE"
    assert "account_build_unverified" in read(scene_env).reason_codes


def test_network_failure_never_persists_raw_remote_message(
    scene_env, wire, redis_client
):
    from sqlmodel import select

    from app.modules.builds.scene_models import SceneEvidence, SceneReadState

    wire[1].append(
        RuntimeError("offline-token-secret scope [2,6] https://private.example/token")
    )
    result = refresh(scene_env, redis_client, "minis")
    assert result.reason_codes == ("scene_refresh_failed",)
    with Session(engine) as session:
        state = session.exec(select(SceneReadState)).one()
        assert (
            state.error_code == "scene_refresh_failed" and state.attempt_token is None
        )
        assert not session.exec(select(SceneEvidence)).all()
        assert "secret" not in json.dumps(state.model_dump(), default=str)


def test_scene_services_reject_foreign_bc_and_viewer_refresh(
    scene_env, wire, redis_client
):
    from dataclasses import replace

    from sqlmodel import select

    from app.core.errors import DomainError
    from app.modules.accounts.models import TenantBC
    from app.modules.tenants.models import TenantMembership

    with Session(engine) as session, session.begin():
        session.add(
            TenantBC(tenant_id=scene_env["context"].tenant_id, bc_id="other-bc")
        )
    with pytest.raises(DomainError) as caught:
        read({**scene_env, "bc_id": "other-bc"})
    assert caught.value.code == "account_access_denied"
    with pytest.raises(DomainError) as caught:
        read({**scene_env, "context": replace(scene_env["context"], tenant_id=uuid4())})
    assert caught.value.code == "tenant_forbidden"
    with Session(engine) as session, session.begin():
        member = session.exec(
            select(TenantMembership).where(
                TenantMembership.user_id == scene_env["context"].actor_id,
                TenantMembership.tenant_id == scene_env["context"].tenant_id,
            )
        ).one()
        member.role = "viewer"
    with pytest.raises(DomainError) as caught:
        refresh(scene_env, redis_client, "minis")
    assert caught.value.code == "action_forbidden"
    assert "account_build_unverified" in read(scene_env).reason_codes
    assert not wire[0]
