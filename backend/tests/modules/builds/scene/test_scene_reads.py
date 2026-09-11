"""Public-document fixtures; real PostgreSQL/Redis, official urllib3 wire only."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlmodel import Session, select

from app.core.credentials import encrypt_credentials
from app.core.db import engine
from app.modules.accounts.connection_models import (
    BCConnectionBinding,
    BCDefaultRoute,
    ConnectionAuthorization,
)
from app.modules.accounts.models import BCAccountAccess, TikTokConnection
from app.modules.accounts.routing import freeze_route
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
        grant.checked_at = datetime.now(UTC)
        if (
            session.get(
                BCConnectionBinding, (conn.tenant_id, source_env["bc_id"], conn.id)
            )
            is None
        ):
            session.add(
                BCConnectionBinding(
                    tenant_id=conn.tenant_id,
                    bc_id=source_env["bc_id"],
                    connection_id=conn.id,
                    kind=conn.kind,
                )
            )
        authorization = session.exec(
            select(ConnectionAuthorization).where(
                ConnectionAuthorization.tenant_id == conn.tenant_id,
                ConnectionAuthorization.connection_id == conn.id,
                ConnectionAuthorization.authorization_revision
                == conn.authorization_revision,
            )
        ).first()
        if authorization is None:
            authorization = ConnectionAuthorization(
                tenant_id=conn.tenant_id,
                connection_id=conn.id,
                authorization_revision=conn.authorization_revision,
                scopes=["2", "6"],
                permission_summary={
                    "read_authorized": True,
                    "build_authorized": True,
                    "upload_authorized": True,
                },
                source="SYNTHETIC_VERIFIED_EVIDENCE",
                verified_at=datetime.now(UTC),
            )
            session.add(authorization)
        # 复用来源 fixture 的同一授权行，并设置本场景实际数字 scope 证据。
        authorization.scopes = ["2", "6"]
        authorization.permission_summary = {
            "read_authorized": True,
            "build_authorized": True,
            "upload_authorized": True,
        }
        authorization.verified_at = datetime.now(UTC)
        session.flush()
        default = session.get(BCDefaultRoute, (conn.tenant_id, source_env["bc_id"]))
        if default is None:
            session.add(
                BCDefaultRoute(
                    tenant_id=conn.tenant_id,
                    bc_id=source_env["bc_id"],
                    connection_id=conn.id,
                )
            )
        else:
            default.connection_id = conn.id
        session.flush()
        source_env["route"] = freeze_route(
            session, context=source_env["context"], bc_id=source_env["bc_id"]
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
            route=env["route"],
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


def test_scene_read_only_reports_missing_facts_without_network(
    database_engine, scene_case, gateway_wire
):
    from sqlmodel import select

    from app.modules.builds.scene import read_scene_context
    from app.modules.builds.scene_job_models import SceneJob

    case = scene_case
    with Session(database_engine) as db:
        result = read_scene_context(
            db,
            context=case["context"],
            bc_id=case["route"].bc_id,
            advertiser_id=case["advertiser_id"],
            link_id=case["link_id"],
            route=case["route"],
        )
        assert not result.supported and "scene_evidence_missing" in result.reason_codes
        assert result.copy_length_limit == 100
        assert (
            db.exec(
                select(SceneJob).where(SceneJob.tenant_id == case["context"].tenant_id)
            ).first()
            is None
        )
    assert gateway_wire["wire"].calls == []
