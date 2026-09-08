from uuid import uuid4

import pytest
from sqlmodel import Session

from app.core.config import settings
from app.core.credentials import encrypt_credentials
from app.core.db import engine
from app.jobs.admission import admission_keys
from app.modules.accounts.models import BCAccountAccess, TikTokConnection
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire


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
