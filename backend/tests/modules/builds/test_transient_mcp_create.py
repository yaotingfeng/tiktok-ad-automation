"""发送前网络中断能继续，已发送的广告仍只能回读。"""

import json
from datetime import UTC, datetime, timedelta

import httpx2
import pytest
from sqlmodel import Session

from app.modules.builds.execution_models import ExecutionStep
from tests.modules.builds.test_channel_execution import (
    app_config as app_config,
)
from tests.modules.builds.test_channel_execution import (
    channel_execution as channel_execution,
)
from tests.modules.builds.test_channel_execution import (
    created,
    invoke,
)
from tests.modules.builds.test_channel_execution import (
    database_engine as database_engine,
)
from tests.modules.builds.test_channel_execution import (
    gateway_case as gateway_case,
)
from tests.modules.builds.test_channel_execution import (
    gateway_wire as gateway_wire,
)
from tests.modules.builds.test_channel_execution import (
    policy as policy,
)
from tests.modules.builds.test_channel_execution import (
    retain_build_history as retain_build_history,
)
from tests.modules.builds.test_channel_execution import (
    scene_case as scene_case,
)


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_MCP"], indirect=True)
@pytest.mark.parametrize("recover", [True, False])
def test_catalog_network_failure_retries_bounded_without_duplicate_create(
    database_engine, redis_client, channel_execution, monkeypatch, recover
):
    from app.integrations.tiktok.mcp import transport
    from tests.integrations.tiktok.gateway_support import business_calls

    case, wire = channel_execution
    original = transport._new_http_transport
    failures = 0

    class InterruptedCatalog(original):
        async def handle_async_request(self, request):
            nonlocal failures
            message = json.loads(request.content) if request.method == "POST" else {}
            if message.get("method") == "tools/list" and (not recover or not failures):
                failures += 1
                raise httpx2.ReadTimeout("synthetic secret must not enter evidence")
            return await super().handle_async_request(request)

    monkeypatch.setattr(transport, "_new_http_transport", InterruptedCatalog)
    created(wire)
    assert invoke(database_engine, redis_client, case) == "PENDING"
    with Session(database_engine) as db, db.begin():
        step = db.get(ExecutionStep, case["step_id"])
        digest = step.request_body_digest
        assert digest and not step.remote_id
        assert step.due_at > datetime.now(UTC)
        step.due_at = datetime.now(UTC) - timedelta(seconds=1)
    if recover:
        assert invoke(database_engine, redis_client, case) == "SUCCEEDED"
        assert invoke(database_engine, redis_client, case) == "SUCCEEDED"
        assert len(business_calls(wire, "OFFICIAL_MCP")) == 1
    else:
        assert invoke(database_engine, redis_client, case) == "PENDING"
        with Session(database_engine) as db, db.begin():
            db.get(ExecutionStep, case["step_id"]).due_at = datetime.now(
                UTC
            ) - timedelta(seconds=1)
        assert invoke(database_engine, redis_client, case) == "FAILED"
        assert invoke(database_engine, redis_client, case) == "FAILED"
        assert not business_calls(wire, "OFFICIAL_MCP")
    with Session(database_engine) as db:
        step = db.get(ExecutionStep, case["step_id"])
        assert step.request_body_digest == digest
        assert "synthetic secret" not in json.dumps(step.resolved)
