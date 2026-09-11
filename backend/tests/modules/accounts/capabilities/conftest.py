import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlmodel import Session, select

from app.core.config import settings
from app.core.credentials import encrypt_credentials
from app.core.db import engine
from app.jobs.admission import admission_keys
from app.modules.accounts.connection_models import (
    BCConnectionBinding,
    ConnectionAuthorization,
)
from app.modules.accounts.models import BCAccountAccess, TikTokConnection
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire


def seed_authority(session, conn, bc_id, scope="[2,6]"):
    values = json.loads(scope) if scope is not None else []
    # 共享源账户夹具已建立冻结路由；复用同源事实，避免重复插入绑定。
    if session.get(BCConnectionBinding, (conn.tenant_id, bc_id, conn.id)) is None:
        session.add(
            BCConnectionBinding(
                tenant_id=conn.tenant_id,
                bc_id=bc_id,
                connection_id=conn.id,
                kind=conn.kind,
            )
        )
    facts = session.exec(
        select(ConnectionAuthorization).where(
            ConnectionAuthorization.tenant_id == conn.tenant_id,
            ConnectionAuthorization.connection_id == conn.id,
            ConnectionAuthorization.authorization_revision
            == conn.authorization_revision,
        )
    ).one_or_none()
    if facts is None:
        facts = ConnectionAuthorization(
            tenant_id=conn.tenant_id,
            connection_id=conn.id,
            authorization_revision=conn.authorization_revision,
        )
    facts.upstream_subject = "synthetic-subject"
    facts.issuer = "https://business-api.tiktok.com"
    facts.resource = "https://business-api.tiktok.com/open_api/v1.3"
    facts.scopes = [str(v) for v in values]
    facts.source = "OFFICIAL_TOKEN_SCOPE"
    facts.verified_at = datetime.now(UTC)
    facts.permission_summary = {
        "read_authorized": True if scope is not None else None,
        "build_authorized": 2 in values if scope is not None else None,
        "upload_authorized": bool(set(values) & {6, 61, 611})
        if scope is not None
        else None,
    }
    session.add(facts)


@pytest.fixture
def capability_env(source_env, monkeypatch, redis_client):
    from app.modules.accounts import capabilities

    monkeypatch.setattr(capabilities, "_require_bounded_worker", lambda: None)
    with Session(engine) as session, session.begin():
        conn = session.get(TikTokConnection, source_env["connection_id"])
        conn.credential_ciphertext = encrypt_credentials(
            tenant_id=conn.tenant_id,
            value={"access_token": "offline-capability-token", "scope": "[2,6]"},
        )
        seed_authority(session, conn, source_env["bc_id"])
        grant = session.get(
            BCAccountAccess,
            (conn.tenant_id, source_env["bc_id"], "actual-account", conn.id),
        )
        grant.permission_state = "UNKNOWN"
        grant.can_build = grant.can_upload = False
    yield {**source_env, "request_id": uuid4()}
    redis_client.delete(
        *admission_keys(
            settings.TIKTOK_APP_ID,
            capabilities.ENDPOINT,
            source_env["context"].tenant_id,
            source_env["bc_id"],
        )
    )
