"""通道版本和租户/BC 归属必须由真实数据库保证。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.modules.accounts.models import (
    AuthorizationAttempt,
    DiscoveryRun,
    TenantBC,
    TikTokConnection,
)


def connection(session, context, kind="OFFICIAL_MCP"):
    row = TikTokConnection(tenant_id=context.tenant_id, kind=kind)
    session.add(row)
    session.flush()
    return row


def test_token_revision_does_not_change_authority(session, context):
    row = TikTokConnection(
        tenant_id=context.tenant_id,
        kind="OFFICIAL_MCP",
        service_profile="tiktok-official",
        credential_revision=2,
        authorization_revision=7,
        adapter_contract_revision="accounts-v1",
    )
    session.add(row)
    session.flush()
    row.credential_revision += 1
    session.flush()
    session.refresh(row)
    assert (row.credential_revision, row.authorization_revision) == (3, 7)
    assert row.kind == "OFFICIAL_MCP"
    assert row.adapter_contract_revision == "accounts-v1"


@pytest.mark.parametrize("field", ["credential_revision", "authorization_revision"])
def test_connection_rejects_negative_revision(session, context, field):
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(TikTokConnection(tenant_id=context.tenant_id, **{field: -1}))
        session.flush()


def test_binding_multiple_bcs_for_both_channels(session, context):
    from app.modules.accounts.connection_models import BCConnectionBinding

    mcp = connection(session, context)
    api = connection(session, context, "OFFICIAL_API")
    for bc in ("bc-a", "bc-b"):
        session.add(TenantBC(tenant_id=context.tenant_id, bc_id=bc))
    session.flush()
    for bc in ("bc-a", "bc-b"):
        session.add(
            BCConnectionBinding(
                tenant_id=context.tenant_id,
                bc_id=bc,
                connection_id=api.id,
                kind="OFFICIAL_API",
            )
        )
    session.add(
        BCConnectionBinding(
            tenant_id=context.tenant_id,
            bc_id="bc-a",
            connection_id=mcp.id,
            kind="OFFICIAL_MCP",
        )
    )
    session.flush()
    session.add(
        BCConnectionBinding(
            tenant_id=context.tenant_id,
            bc_id="bc-b",
            connection_id=mcp.id,
            kind="OFFICIAL_MCP",
        )
    )
    session.flush()
    # 同一连接的通道不能伪装为 API。
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            BCConnectionBinding(
                tenant_id=context.tenant_id,
                bc_id="bc-b",
                connection_id=mcp.id,
                kind="OFFICIAL_API",
            )
        )
        session.flush()


def test_default_requires_exact_tenant_bc_binding(session, context, other_context):
    from app.modules.accounts.connection_models import (
        BCConnectionBinding,
        BCDefaultRoute,
    )

    row = connection(session, context)
    for owner in (context, other_context):
        session.add(TenantBC(tenant_id=owner.tenant_id, bc_id="same-bc"))
    session.flush()
    session.add(
        BCConnectionBinding(
            tenant_id=context.tenant_id,
            bc_id="same-bc",
            connection_id=row.id,
            kind="OFFICIAL_MCP",
        )
    )
    session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            BCDefaultRoute(
                tenant_id=other_context.tenant_id, bc_id="same-bc", connection_id=row.id
            )
        )
        session.flush()
    session.add(
        BCDefaultRoute(
            tenant_id=context.tenant_id, bc_id="same-bc", connection_id=row.id
        )
    )
    session.flush()


def mcp_attempt(session, context, row):
    from app.modules.accounts.connection_models import McpAuthorizationAttempt

    attempt = McpAuthorizationAttempt(
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        connection_id=row.id,
        issuer="https://issuer.example",
        resource="https://resource.example/mcp",
        redirect_uri="https://app.example/callback",
        state_hash=uuid4().hex,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    session.add(attempt)
    session.flush()
    return attempt


def test_mcp_candidate_has_own_fk_and_cannot_mix_api_candidate(
    session, context, other_context
):
    row = connection(session, context)
    attempt = mcp_attempt(session, context, row)
    api = AuthorizationAttempt(
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        connection_id=row.id,
        state_hash=uuid4().hex,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    session.add(api)
    session.flush()
    run = DiscoveryRun(
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        connection_id=row.id,
        mcp_candidate_attempt_id=attempt.id,
    )
    session.add(run)
    session.flush()
    assert run.candidate_attempt_id is None
    with pytest.raises(IntegrityError), session.begin_nested():
        run.candidate_attempt_id = api.id
        session.flush()
    other = connection(session, other_context)
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            DiscoveryRun(
                tenant_id=other_context.tenant_id,
                actor_id=other_context.actor_id,
                connection_id=other.id,
                mcp_candidate_attempt_id=attempt.id,
            )
        )
        session.flush()


def test_authorization_keeps_missing_upstream_facts_unknown(session, context):
    from app.modules.accounts.connection_models import ConnectionAuthorization

    row = connection(session, context)
    facts = ConnectionAuthorization(
        tenant_id=context.tenant_id,
        connection_id=row.id,
        authorization_revision=1,
        issuer="https://issuer.example",
        resource="https://resource.example/mcp",
        scopes=["mcp:tt4b"],
        source="oauth-token-response",
        access_token_expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    session.add(facts)
    session.flush()
    session.refresh(facts)
    assert facts.upstream_subject is None and facts.upstream_grant_id is None
    assert facts.permission_summary == {}
    assert facts.access_token_expires_at > datetime.now(UTC)


def test_observations_and_refresh_attempt_are_tenant_and_connection_bound(
    session, context, other_context
):
    from app.modules.accounts.connection_models import (
        ConnectionToolObservation,
        McpRefreshAttempt,
    )

    row = connection(session, context)
    attempt = mcp_attempt(session, context, row)
    other = connection(session, other_context)
    for values in (
        {"tenant_id": other_context.tenant_id, "connection_id": row.id},
        {
            "tenant_id": other_context.tenant_id,
            "connection_id": other.id,
            "candidate_attempt_id": attempt.id,
        },
    ):
        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(
                ConnectionToolObservation(
                    **values, schema_digest="abc", expected_contract_revision="v1"
                )
            )
            session.flush()
    refresh = McpRefreshAttempt(
        tenant_id=context.tenant_id,
        connection_id=row.id,
        base_credential_revision=2,
        base_authorization_revision=7,
        status="OUTCOME_UNKNOWN",
    )
    session.add(refresh)
    session.flush()
    session.refresh(refresh)
    assert refresh.status == "OUTCOME_UNKNOWN" and refresh.candidate_ciphertext is None
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            McpRefreshAttempt(
                tenant_id=other_context.tenant_id,
                connection_id=row.id,
                base_credential_revision=2,
                base_authorization_revision=7,
            )
        )
        session.flush()


def test_unknown_refresh_outcome_fences_same_token_revision(session, context):
    from app.modules.accounts.connection_models import McpRefreshAttempt

    row = connection(session, context)
    session.add(
        McpRefreshAttempt(
            tenant_id=context.tenant_id,
            connection_id=row.id,
            base_credential_revision=2,
            base_authorization_revision=7,
            status="OUTCOME_UNKNOWN",
        )
    )
    session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            McpRefreshAttempt(
                tenant_id=context.tenant_id,
                connection_id=row.id,
                base_credential_revision=2,
                base_authorization_revision=7,
                status="REQUEST_ARMED",
                request_armed_at=datetime.now(UTC),
            )
        )
        session.flush()
    # 新修订仍可记录下一轮刷新；旧未知结果不会被覆盖。
    session.add(
        McpRefreshAttempt(
            tenant_id=context.tenant_id,
            connection_id=row.id,
            base_credential_revision=3,
            base_authorization_revision=7,
        )
    )
    session.flush()


def test_mcp_candidate_cannot_reference_another_connection_in_same_tenant(
    session, context
):
    row = connection(session, context)
    other = connection(session, context)
    attempt = mcp_attempt(session, context, other)
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            DiscoveryRun(
                tenant_id=context.tenant_id,
                actor_id=context.actor_id,
                connection_id=row.id,
                mcp_candidate_attempt_id=attempt.id,
            )
        )
        session.flush()
