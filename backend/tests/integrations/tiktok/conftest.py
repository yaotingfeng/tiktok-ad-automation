from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import httpx2
import pytest

from app.integrations.tiktok.mcp.protocol import ToolContract
from tests.integrations.tiktok.mcp_wire import McpWire


@pytest.fixture
def fixture_contract():
    return ToolContract(
        operation="builds.create_campaign",
        tool_name="fixture_create",
        effect="WRITE",
        input_schema={
            "type": "object",
            "properties": {"advertiser_id": {"type": "string"}},
            "required": ["advertiser_id"],
            "additionalProperties": False,
        },
        output_schema=None,
        response_shape="OBJECT",
        source_urls=(),
        evidence="OBSERVED",
    )


@pytest.fixture
def mcp_wire(fixture_contract):
    wire = McpWire(
        [
            {
                "name": fixture_contract.tool_name,
                "inputSchema": fixture_contract.input_schema,
            }
        ]
    )
    yield wire
    wire.close()


@pytest.fixture
def client_factory(monkeypatch, mcp_wire, fixture_contract):
    from app.integrations.tiktok.mcp import transport

    class LocalTransport(httpx2.AsyncBaseTransport):
        def __init__(self):
            self.inner = httpx2.AsyncHTTPTransport(retries=0)

        async def handle_async_request(self, request):
            # 仅协议边界改路由，生产客户端始终构造固定官方 URL。
            request.url = httpx2.URL(mcp_wire.url)
            return await self.inner.handle_async_request(request)

        async def aclose(self):
            await self.inner.aclose()

    monkeypatch.setattr(transport, "_new_http_transport", LocalTransport)
    events = []

    def authorize(advertiser_id, operation):
        events.append(("authorize", advertiser_id, operation))
        assert advertiser_id in (None, "123")

    @contextmanager
    def admit(advertiser_id, operation):
        events.append(("enter", advertiser_id, operation))
        try:
            yield
        finally:
            events.append(("exit", advertiser_id, operation))

    def factory(**overrides):
        kwargs = {
            "token": "synthetic-bearer-secret",
            "task_deadline": datetime.now(UTC) + timedelta(seconds=5),
            "authorize": authorize,
            "admit": admit,
            "contracts": {fixture_contract.operation: fixture_contract},
            "observed_tools": {fixture_contract.tool_name: mcp_wire.tools[0]},
        }
        kwargs.update(overrides)
        return transport.open_bound_mcp_client(**kwargs)

    factory.events = events
    return factory


@pytest.fixture
def bound_client(client_factory):
    with client_factory() as client:
        yield client
