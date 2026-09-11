"""同一执行快照的 API/MCP 核查，只有 HTTP 传输被替换。"""

# ruff: noqa: F401,F811 -- shared real PG/Redis and HTTP fixtures

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlmodel import Session, select

from app.core.config import settings
from app.core.credentials import decrypt_credentials, encrypt_credentials
from app.modules.accounts.models import TikTokConnection
from app.modules.builds import reconciliation
from app.modules.builds.execution_models import ExecutionStep
from app.modules.builds.historical_read import (
    authorize_historical_read,
    process_historical_read,
)
from app.modules.builds.recovery_routes import BuildHistoricalRead
from app.modules.tenants.models import TenantMembership
from tests.integrations.tiktok.build_wire import BuildWire
from tests.modules.builds.test_channel_execution import (
    app_config,
    channel_execution,
    database_engine,
    gateway_case,
    gateway_wire,
    policy,
    retain_build_history,
    scene_case,
)
from tests.modules.builds.test_historical_read import renewed
from tests.modules.builds.test_reconciliation import arm


@pytest.fixture
def history_case(channel_execution, database_engine, redis_client, monkeypatch):
    case, wire = channel_execution
    monkeypatch.setattr(
        reconciliation,
        "current_task",
        SimpleNamespace(
            request=SimpleNamespace(
                timelimit=(45, 40), called_directly=False, is_eager=False
            )
        ),
    )
    monkeypatch.setattr(
        reconciliation,
        "current_process",
        lambda: SimpleNamespace(daemon=True, name="ForkPoolWorker-1"),
    )
    env = SimpleNamespace(
        engine=database_engine,
        context=case["context"],
        ids=case["ids"],
        redis=redis_client,
    )
    return env, wire, case["route"]


def response(wire, kind, rows, *, page=1, total=None):
    data = {
        "list": rows,
        "page_info": {
            "page": page,
            "page_size": 100,
            "total_number": len(rows) if total is None else total,
            "total_page": ((len(rows) if total is None else total) + 99) // 100,
        },
    }
    wire["sdk_data"]["data"] = data
    tool = {
        "CAMPAIGN": "smart_plus_campaign_get",
        "ADGROUP": "smart_plus_adgroup_get",
        "ADGROUP_STATUS": "adgroup_get",
        "AD": "smart_plus_ad_get",
    }[kind]
    wire["wire"].results[tool].append(
        {
            "content": [],
            "structuredContent": {
                "code": 0,
                "data": data,
                "request_id": "safe-readback",
            },
        }
    )


def process(env, identity, revision=0):
    process_historical_read(
        database_engine=env.engine,
        redis_client=env.redis,
        context=env.context,
        payload={"read_id": str(identity), "revision": revision},
    )


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_dual_channel_historical_scan_survives_rotation_and_never_resumes_children(
    history_case,
):
    env, wire, _ = history_case
    step_id, body = arm(env, "AD")
    route = renewed(env, step_id)
    with Session(env.engine) as session, session.begin():
        identity = authorize_historical_read(
            session,
            context=env.context,
            source_step_id=step_id,
            new_route=route,
            request_id=uuid4(),
        )
        originals = {
            s.id: s.model_dump(mode="json")
            for s in session.exec(
                select(ExecutionStep).where(
                    ExecutionStep.submission_id
                    == session.get(ExecutionStep, step_id).submission_id
                )
            ).all()
        }
    response(
        wire,
        "AD",
        [
            {**body, "ad_name": f"different-{i}", "smart_plus_ad_id": f"other-{i}"}
            for i in range(100)
        ],
        total=101,
    )
    process(env, identity)
    with Session(env.engine) as session, session.begin():
        audit = session.get(BuildHistoricalRead, identity)
        assert audit.status == "PENDING" and audit.progress["page"] == 2, (
            audit.status,
            audit.error_code,
        )
        connection = session.get(TikTokConnection, route.connection_id)
        material = decrypt_credentials(
            tenant_id=env.context.tenant_id, ciphertext=connection.credential_ciphertext
        )
        material["access_token"] = "synthetic-rotated-token"
        connection.credential_ciphertext = encrypt_credentials(
            tenant_id=env.context.tenant_id, value=material
        )
        connection.credential_revision += 1
        session.add(connection)
    response(
        wire,
        "AD",
        [{**body, "smart_plus_ad_id": "historical-match"}],
        page=2,
        total=101,
    )
    process(env, identity, 1)
    process(env, identity, 1)
    with Session(env.engine) as session:
        audit = session.get(BuildHistoricalRead, identity)
        assert (audit.status, audit.remote_id) == ("CONFIRMED", "historical-match"), (
            audit.error_code,
            audit.progress.get("page"),
        )
        assert {
            s.id: s.model_dump(mode="json")
            for s in session.exec(
                select(ExecutionStep).where(
                    ExecutionStep.submission_id == audit.submission_id
                )
            ).all()
        } == originals


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_MCP"], indirect=True)
@pytest.mark.parametrize("change", ["membership", "claim", "authorization"])
def test_mcp_protocol_boundary_rechecks_admin_claim_and_original_authorization(
    history_case, change
):
    env, wire, _ = history_case
    step_id, body = arm(env)
    route = renewed(env, step_id)
    with Session(env.engine) as session, session.begin():
        identity = authorize_historical_read(
            session,
            context=env.context,
            source_step_id=step_id,
            new_route=route,
            request_id=uuid4(),
        )
    response(wire, "CAMPAIGN", [{**body, "campaign_id": "must-not-be-read"}])

    def mutate_after_first_authorize():
        wire["before"]["callback"] = None
        with Session(env.engine) as session, session.begin():
            if change == "membership":
                member = session.get(
                    TenantMembership, (env.context.tenant_id, env.context.actor_id)
                )
                member.role = "viewer"
                session.add(member)
            elif change == "claim":
                row = session.get(BuildHistoricalRead, identity)
                row.claim_token = uuid4()
                session.add(row)
            else:
                connection = session.get(TikTokConnection, route.connection_id)
                connection.authorization_revision += 1
                session.add(connection)

    wire["before"]["callback"] = mutate_after_first_authorize
    process(env, identity)
    assert any(
        call.get("method") in {"server/discover", "initialize"}
        for call in wire["wire"].calls
    )
    assert wire["before"]["callback"] is None
    assert not [
        call for call in wire["wire"].calls if call.get("method") == "tools/call"
    ]
    with Session(env.engine) as session:
        assert session.get(ExecutionStep, step_id).status == "UNKNOWN"
        assert session.get(BuildHistoricalRead, identity).remote_id is None


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_MCP"], indirect=True)
def test_mcp_ordinary_read_uses_armed_request_correlation_without_recreating(
    history_case,
    monkeypatch,
):
    from app.core.config import settings

    env, wire, route = history_case
    for key in ("TIKTOK_APP_ID", "TIKTOK_APP_SECRET", "TIKTOK_REDIRECT_URI"):
        monkeypatch.setattr(settings, key, "")
    step_id, body = arm(env)
    response(wire, "CAMPAIGN", [{**body, "campaign_id": "original-match"}])
    result = reconciliation.process_reconciliation(
        database_engine=env.engine,
        redis_client=env.redis,
        context=env.context,
        step_id=step_id,
        revision=0,
    )
    assert result.state == "SUCCEEDED"
    business = [
        call for call in wire["wire"].calls if call.get("method") == "tools/call"
    ]
    assert (
        len(business) == 1
        and business[0]["params"]["name"] == "smart_plus_campaign_get"
    )
    with Session(env.engine) as session:
        assert session.get(ExecutionStep, step_id).request_body == body


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_MCP"], indirect=True)
@pytest.mark.parametrize("unknown", [False, True])
def test_historical_refresh_wait_resumes_same_read_but_unknown_is_terminal(
    history_case, unknown
):
    from app.modules.accounts.connection_models import McpRefreshAttempt

    env, wire, _ = history_case
    step_id, body = arm(env)
    route = renewed(env, step_id)
    with Session(env.engine) as session, session.begin():
        identity = authorize_historical_read(
            session,
            context=env.context,
            source_step_id=step_id,
            new_route=route,
            request_id=uuid4(),
        )
        connection = session.get(TikTokConnection, route.connection_id)
        material = decrypt_credentials(
            tenant_id=env.context.tenant_id, ciphertext=connection.credential_ciphertext
        )
        material["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        connection.credential_ciphertext = encrypt_credentials(
            tenant_id=env.context.tenant_id, value=material
        )
        session.add(connection)
        if unknown:
            session.add(
                McpRefreshAttempt(
                    tenant_id=env.context.tenant_id,
                    connection_id=route.connection_id,
                    base_credential_revision=connection.credential_revision,
                    base_authorization_revision=route.authorization_revision,
                    status="OUTCOME_UNKNOWN",
                    request_armed_at=datetime.now(UTC),
                )
            )
    process(env, identity)
    assert not wire["wire"].calls
    with Session(env.engine) as session, session.begin():
        audit = session.get(BuildHistoricalRead, identity)
        assert audit.status == ("BLOCKED" if unknown else "PENDING")
        assert audit.error_code == (
            "mcp_refresh_unknown" if unknown else "mcp_refresh_pending"
        )
        if unknown:
            assert audit.dispatch_id is None
            return
        assert audit.dispatch_revision == 1 and audit.progress["page"] == 1
        attempt = session.exec(
            select(McpRefreshAttempt).where(
                McpRefreshAttempt.connection_id == route.connection_id
            )
        ).one()
        attempt.status = "PUBLISHED"
        session.add(attempt)
        connection = session.get(TikTokConnection, route.connection_id)
        material["access_token"] = "synthetic-published-refresh"
        material["expires_at"] = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        connection.credential_ciphertext = encrypt_credentials(
            tenant_id=env.context.tenant_id, value=material
        )
        connection.credential_revision += 1
        session.add(connection)
        audit.due_at = datetime.now(UTC)
        session.add(audit)
    response(wire, "CAMPAIGN", [{**body, "campaign_id": "after-refresh"}])
    process(env, identity, 1)
    with Session(env.engine) as session:
        assert session.get(BuildHistoricalRead, identity).status == "CONFIRMED"
        assert session.get(ExecutionStep, step_id).status == "UNKNOWN"


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_MCP"], indirect=True)
def test_mcp_wrong_original_attempt_correlation_never_reads(history_case):
    env, wire, _ = history_case
    step_id, body = arm(env, mcp_request_id=str(uuid4()))
    result = reconciliation.process_reconciliation(
        database_engine=env.engine,
        redis_client=env.redis,
        context=env.context,
        step_id=step_id,
        revision=0,
    )
    assert result.state == "UNKNOWN" and not wire["wire"].calls
    with Session(env.engine) as session:
        source = session.get(ExecutionStep, step_id)
        assert (
            source.error_code == "readback_intent_incomplete"
            and source.request_body == body
        )
