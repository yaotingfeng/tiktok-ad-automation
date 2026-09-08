"""Official OAuth scope is private, validated capability evidence, not a DTO."""

import json

import pytest
from urllib3.response import HTTPResponse

from app.core.credentials import decrypt_credentials
from app.core.errors import DomainError
from app.integrations.tiktok import auth


@pytest.mark.parametrize("scope", [[6, 2, 2], [], None])
def test_real_sdk_token_receipt_keeps_only_validated_scope(monkeypatch, scope):
    data = {
        "access_token": "scope-fixture-token",
        "scope": scope,
        "secret_extra": "drop",
    }
    if scope is None:
        del data["scope"]
    monkeypatch.setattr(
        "urllib3.PoolManager.request",
        lambda *a, **k: HTTPResponse(
            body=json.dumps({"code": 0, "data": data}).encode(), status=200
        ),
    )
    receipt = auth._token_request(
        app_id="fake-app", secret="fake-secret", auth_code="fake-code"
    )
    assert receipt == (
        {"access_token": "scope-fixture-token"}
        if scope is None
        else {
            "access_token": "scope-fixture-token",
            "scope": json.dumps(sorted(set(scope)), separators=(",", ":")),
        }
    )


@pytest.mark.parametrize(
    "scope", [True, "2,6", [True], [0], [-1], [2.0], ["2"], list(range(1, 1026))]
)
def test_malformed_scope_cannot_become_permission_evidence(monkeypatch, scope):
    monkeypatch.setattr(
        "urllib3.PoolManager.request",
        lambda *a, **k: HTTPResponse(
            body=json.dumps(
                {"code": 0, "data": {"access_token": "private", "scope": scope}}
            ).encode(),
            status=200,
        ),
    )
    with pytest.raises(DomainError) as raised:
        auth._token_request(
            app_id="fake-app", secret="fake-secret", auth_code="fake-code"
        )
    assert raised.value.code == "invalid_token_response"
    assert "private" not in str(raised.value)


@pytest.mark.usefixtures("sdk_transport")
def test_candidate_scope_stays_encrypted_and_outside_dispatch(
    session, auth_attempt, redis_client, policy, monkeypatch
):
    from datetime import UTC, datetime

    from sqlmodel import select

    from app.jobs.models import PendingDispatch

    monkeypatch.setattr(
        "urllib3.PoolManager.request",
        lambda *a, **k: HTTPResponse(
            body=b'{"code":0,"data":{"access_token":"private","scope":[2,6]}}',
            status=200,
        ),
    )
    auth.finish_authorization(
        session,
        state="fake-state",
        auth_code="fake-code",
        now=datetime.now(UTC),
        redis_client=redis_client,
        policy=policy,
    )
    session.refresh(auth_attempt)
    assert decrypt_credentials(
        tenant_id=auth_attempt.tenant_id, ciphertext=auth_attempt.candidate_ciphertext
    ) == {"access_token": "private", "scope": "[2,6]"}
    dispatch = session.exec(
        select(PendingDispatch).where(
            PendingDispatch.tenant_id == auth_attempt.tenant_id
        )
    ).one()
    assert set(dispatch.payload) == {"attempt_id"}
    assert "private" not in repr(dispatch) and "scope" not in repr(dispatch)
