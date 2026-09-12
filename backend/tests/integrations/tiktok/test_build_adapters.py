"""真实 SDK/MCP 客户端，仅在 HTTP 传输边界改向本地服务。"""

import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx2
import pytest
import urllib3

from app.integrations.tiktok.contracts.builds import BuildReadQuery
from app.integrations.tiktok.contracts.common import RemoteCallError
from app.integrations.tiktok.mcp.protocol import load_tool_contracts
from app.integrations.tiktok.mcp.transport import open_bound_mcp_client
from app.integrations.tiktok.sdk import official_client
from app.modules.builds.readback_compare import compare_record
from app.modules.builds.request_compiler import decode_intent, remote_request_id
from tests.contracts.test_tiktok_build_contract import build_bodies


@pytest.mark.parametrize(
    "kind,field,id_key",
    [("CAMPAIGN", "budget", "campaign_id"), ("ADGROUP", "roas_bid", "adgroup_id")],
)
def test_remote_raw_numeric_amount_cannot_match_after_float_rounding(
    build_case, kind, field, id_key
):
    adapter, wire, _ = build_case
    body = {**build_bodies()[kind], field: "100.01"}
    query = BuildReadQuery(intent=decode_intent(kind, body), remote_id="remote-1")
    envelope = {
        "code": 0,
        "data": {
            "list": [{**body, id_key: "remote-1"}],
            "page_info": {
                "page": 1,
                "page_size": 100,
                "total_number": 1,
                "total_page": 1,
            },
        },
        "request_id": "raw-number-request",
    }
    raw = (
        json.dumps(envelope)
        .replace(f'"{field}": "100.01"', f'"{field}": 100.010000000000000001')
        .encode()
    )
    wire.enqueue_raw_envelope(kind, raw)
    page = adapter.read_page(query=query)
    record = page.rows[0]
    assert compare_record(query=query, record=record) == "INCOMPLETE"
    assert record.intent is None and field in record.missing_fields
    assert record.operation_status == "ENABLE"
    assert len(wire.business_calls()) == 1


@pytest.mark.parametrize(
    "kind,field,id_key",
    [("CAMPAIGN", "budget", "campaign_id"), ("ADGROUP", "roas_bid", "adgroup_id")],
)
def test_remote_raw_integer_amount_remains_exact_evidence(
    build_case, kind, field, id_key
):
    adapter, wire, _ = build_case
    body = {**build_bodies()[kind], field: "100"}
    query = BuildReadQuery(intent=decode_intent(kind, body), remote_id="remote-1")
    envelope = {
        "code": 0,
        "data": {
            "list": [{**body, id_key: "remote-1"}],
            "page_info": {
                "page": 1,
                "page_size": 100,
                "total_number": 1,
                "total_page": 1,
            },
        },
    }
    raw = json.dumps(envelope).replace(f'"{field}": "100"', f'"{field}": 100').encode()
    wire.enqueue_raw_envelope(kind, raw)
    record = adapter.read_page(query=query).rows[0]
    assert compare_record(query=query, record=record) == "MATCH"
    assert len(wire.business_calls()) == 1


@pytest.fixture(params=["OFFICIAL_API", "OFFICIAL_MCP"])
def build_case(request, monkeypatch):
    from app.integrations.tiktok.adapters.mcp_builds import McpBuildOperations
    from app.integrations.tiktok.adapters.sdk_builds import ApiBuildOperations
    from app.integrations.tiktok.mcp import transport
    from tests.integrations.tiktok.build_wire import BuildWire

    wire = BuildWire(request.param)
    deadline = datetime.now(UTC) + timedelta(seconds=20)
    events = []

    def authorize(advertiser_id, operation):
        events.append((advertiser_id, operation))
        assert advertiser_id is None or advertiser_id == "90071992547409939999"

    @contextmanager
    def admit(_advertiser_id, _operation):
        yield

    @contextmanager
    def scope(advertiser_id, operation, actual_deadline):
        assert actual_deadline == deadline
        authorize(advertiser_id, operation)
        yield

    try:
        if request.param == "OFFICIAL_API":
            original = urllib3.PoolManager.request

            def redirect(pool, method, url, **kwargs):
                assert url.startswith("https://business-api.tiktok.com/")
                return original(
                    pool,
                    method,
                    wire.endpoint + url.removeprefix("https://business-api.tiktok.com"),
                    **kwargs,
                )

            monkeypatch.setattr(urllib3.PoolManager, "request", redirect)
            with official_client(access_token="synthetic-token") as client:
                yield (
                    ApiBuildOperations(client, request_scope=scope, deadline=deadline),
                    wire,
                    events,
                )
        else:

            class LocalTransport(httpx2.AsyncBaseTransport):
                def __init__(self):
                    self.inner = httpx2.AsyncHTTPTransport(retries=0)

                async def handle_async_request(self, request):
                    request.url = httpx2.URL(wire.endpoint)
                    return await self.inner.handle_async_request(request)

                async def aclose(self):
                    await self.inner.aclose()

            monkeypatch.setattr(transport, "_new_http_transport", LocalTransport)
            contracts = {
                contract.operation: contract
                for contract in load_tool_contracts()
                if contract.operation
                in {*wire.operations.values(), *wire.create_operations.values()}
            }
            with open_bound_mcp_client(
                token="synthetic-token",
                task_deadline=deadline,
                authorize=authorize,
                admit=admit,
                contracts=contracts,
                observed_tools={item["name"]: item for item in wire.mcp.tools},
            ) as client:
                yield McpBuildOperations(client), wire, events
    finally:
        wire.close()


@pytest.mark.parametrize(
    "kind,id_key",
    [
        ("CAMPAIGN", "campaign_id"),
        ("ADGROUP", "adgroup_id"),
        ("AD", "smart_plus_ad_id"),
        ("CTA", "creative_portfolio_id"),
    ],
)
def test_real_read_adapters_preserve_complete_intent_and_exact_ids(
    build_case, kind, id_key
):
    adapter, wire, events = build_case
    body = build_bodies()[kind]
    query = BuildReadQuery(
        intent=decode_intent(kind, body), remote_id="90071992547409938888"
    )
    wire.enqueue_readback(kind, [{**body, id_key: query.remote_id}], page=1, total=1)
    page = adapter.read_page(query=query)
    assert page.rows[0].remote_id == "90071992547409938888"
    assert compare_record(query=query, record=page.rows[0]) == "MATCH"
    assert page.evidence.request_id == "synthetic-read-request"
    call = wire.business_calls()[0]
    arguments = call["arguments"]
    assert arguments["advertiser_id"] == body["advertiser_id"]
    if kind == "AD":
        assert arguments["filtering"] == {
            "smart_plus_ad_ids": [query.remote_id],
            "adgroup_ids": [body["adgroup_id"]],
        }
    elif kind == "ADGROUP":
        assert arguments["filtering"] == {
            "adgroup_ids": [query.remote_id],
            "campaign_ids": [body["campaign_id"]],
        }
    assert (body["advertiser_id"], wire.operations[kind]) in events


def test_unknown_cta_id_is_not_sent(build_case):
    adapter, wire, _ = build_case
    with pytest.raises(RemoteCallError) as error:
        adapter.read_page(
            query=BuildReadQuery(intent=decode_intent("CTA", build_bodies()["CTA"]))
        )
    assert error.value.effect == "NOT_SENT" and error.value.code == "cta_unknown_id"
    assert wire.business_calls() == []


def test_adgroup_status_read_uses_regular_endpoint_and_returned_scope(build_case):
    adapter, wire, events = build_case
    advertiser = "90071992547409939999"
    wire.enqueue_readback(
        "ADGROUP_STATUS",
        [
            {
                "advertiser_id": advertiser,
                "adgroup_id": "group-1",
                "operation_status": "DISABLE",
            }
        ],
        page=1,
        total=1,
    )
    result = adapter.read_adgroup_status(advertiser_id=advertiser, adgroup_id="group-1")
    assert result.advertiser_id == advertiser and result.adgroup_id == "group-1"
    assert result.operation_status == "DISABLE"
    assert (advertiser, "build.get_regular_adgroups") in events
    assert wire.business_calls()[0]["arguments"]["filtering"] == {
        "adgroup_ids": ["group-1"]
    }


@pytest.mark.parametrize(
    "problem", ["duplicate", "missing_page", "legacy_ad_id", "numeric_id"]
)
def test_incomplete_or_invalid_pages_never_become_evidence(build_case, problem):
    adapter, wire, _ = build_case
    body = build_bodies()["AD"]
    row = {**body, "smart_plus_ad_id": "ad-1"}
    rows, page, total = [row], 1, 1
    if problem == "duplicate":
        rows, total = [row, row], 2
    elif problem == "missing_page":
        page = 2
    elif problem == "legacy_ad_id":
        del row["smart_plus_ad_id"]
        row["ad_id"] = "ad-1"
    else:
        row["smart_plus_ad_id"] = 90071992547409939999
    wire.enqueue_readback("AD", rows, page=page, total=total)
    with pytest.raises(RemoteCallError) as error:
        adapter.read_page(query=BuildReadQuery(intent=decode_intent("AD", body)))
    assert error.value.effect == "UNKNOWN"
    assert len(wire.business_calls()) == 1


def test_missing_cta_scope_is_not_filled_from_request(build_case):
    adapter, wire, _ = build_case
    body = build_bodies()["CTA"]
    query = BuildReadQuery(intent=decode_intent("CTA", body), remote_id="portfolio-1")
    row = {**body, "creative_portfolio_id": "portfolio-1"}
    del row["advertiser_id"]
    wire.enqueue_readback("CTA", [row], page=1, total=1)
    record = adapter.read_page(query=query).rows[0]
    assert record.intent is None and record.missing_fields == ("advertiser_id",)
    assert compare_record(query=query, record=record) == "INCOMPLETE"


@pytest.mark.parametrize("scope", ["foreign", "missing"])
def test_status_supplement_never_fills_or_overrides_response_scope(build_case, scope):
    adapter, wire, _ = build_case
    row = {
        "advertiser_id": "another-advertiser",
        "adgroup_id": "group-1",
        "operation_status": "ENABLE",
    }
    if scope == "missing":
        del row["advertiser_id"]
    wire.enqueue_readback("ADGROUP_STATUS", [row], page=1, total=1)
    with pytest.raises(RemoteCallError):
        adapter.read_adgroup_status(
            advertiser_id="90071992547409939999", adgroup_id="group-1"
        )
    assert len(wire.business_calls()) == 1


@pytest.mark.parametrize("code", [40002, True, "0"])
def test_structured_error_is_unknown_and_never_retried(build_case, code):
    adapter, wire, _ = build_case
    wire.enqueue_envelope(
        "CAMPAIGN",
        {
            "code": code,
            "message": "synthetic-secret",
            "request_id": "safe-error",
            "data": {},
        },
    )
    with pytest.raises(RemoteCallError) as error:
        adapter.read_page(
            query=BuildReadQuery(
                intent=decode_intent("CAMPAIGN", build_bodies()["CAMPAIGN"])
            )
        )
    assert error.value.effect == "UNKNOWN" and not error.value.retryable
    assert "synthetic-secret" not in str(error.value)
    assert len(wire.business_calls()) == 1


def test_multi_page_queries_keep_parent_and_each_page_must_be_complete(build_case):
    adapter, wire, _ = build_case
    body = build_bodies()["ADGROUP"]
    intent = decode_intent("ADGROUP", body)
    wire.enqueue_readback(
        "ADGROUP",
        [{**body, "adgroup_id": f"group-{index}"} for index in range(100)],
        page=1,
        total=101,
    )
    wire.enqueue_readback(
        "ADGROUP", [{**body, "adgroup_id": "group-last"}], page=2, total=101
    )
    first = adapter.read_page(query=BuildReadQuery(intent=intent))
    last = adapter.read_page(query=BuildReadQuery(intent=intent, page=2))
    assert (
        first.complete
        and last.complete
        and len(first.rows) == 100
        and len(last.rows) == 1
    )
    assert first.total_pages == last.total_pages == 2
    calls = wire.business_calls()
    assert [call["arguments"]["page"] for call in calls] == [1, 2]
    assert all(
        call["arguments"]["filtering"]
        == {"adgroup_name": "冻结广告组", "campaign_ids": [body["campaign_id"]]}
        for call in calls
    )


@pytest.mark.parametrize("build_case", ["OFFICIAL_MCP"], indirect=True)
@pytest.mark.parametrize("problem", ["text_only", "contradictory", "schema"])
def test_mcp_build_reads_obey_actual_tool_and_text_envelope_contract(
    build_case, problem
):
    adapter, wire, _ = build_case
    import json

    tool = wire.contracts["build.get_campaigns"].tool_name
    envelope = {
        "code": 0,
        "data": {
            "list": [],
            "page_info": {
                "page": 1,
                "page_size": 100,
                "total_number": 0,
                "total_page": 0,
            },
        },
    }
    if problem == "schema":
        wire.mcp.tools = [
            {**item, "inputSchema": {"type": "object", "properties": {}}}
            if item["name"] == tool
            else item
            for item in wire.mcp.tools
        ]
    else:
        wire.mcp.results[tool].append(
            {
                "content": [{"type": "text", "text": json.dumps(envelope)}],
                **(
                    {"structuredContent": {"code": 40002, "data": {}}}
                    if problem == "contradictory"
                    else {}
                ),
            }
        )
    query = BuildReadQuery(intent=decode_intent("CAMPAIGN", build_bodies()["CAMPAIGN"]))
    if problem == "text_only":
        assert adapter.read_page(query=query) is not None
    else:
        with pytest.raises(RemoteCallError) as error:
            adapter.read_page(query=query)
        assert error.value.effect == ("NOT_SENT" if problem == "schema" else "UNKNOWN")
    assert len(wire.business_calls()) == (0 if problem == "schema" else 1)


@pytest.mark.parametrize("kind", ["CAMPAIGN", "ADGROUP", "AD", "CTA"])
def test_create_uses_exact_frozen_fields_and_real_receipt_without_status_fill(
    build_case, kind
):
    adapter, wire, events = build_case
    body = build_bodies()[kind]
    if kind == "ADGROUP":
        body["roas_bid"] = "1.25"
    intent = decode_intent(kind, body)
    wire.enqueue_created(kind, "90071992547409938888")
    attempt_id = uuid4()
    result = adapter.create(attempt_id=attempt_id, intent=intent)
    assert result.kind == kind and result.remote_id == "90071992547409938888"
    assert result.operation_status is None
    assert result.evidence.request_id == "synthetic-create-request"
    calls = wire.business_calls()
    assert len(calls) == 1
    expected = dict(body)
    for key in ("budget", "roas_bid"):
        if key in expected:
            expected[key] = float(expected[key])
    if wire.channel == "OFFICIAL_MCP" and kind in {"CAMPAIGN", "ADGROUP"}:
        expected["request_id"] = remote_request_id(attempt_id)
    assert calls[0]["arguments"] == expected
    assert (body["advertiser_id"], wire.create_operations[kind]) in events


@pytest.mark.parametrize(
    "problem", ["lost", "code", "boolean_code", "missing_id", "numeric_id", "unsafe_id"]
)
def test_sent_create_uncertainty_never_retries_and_retains_safe_evidence(
    build_case, problem
):
    adapter, wire, _ = build_case
    if problem == "lost":
        wire.drop_created_response("CTA")
    else:
        data = {"creative_portfolio_id": "remote"}
        code = 0
        if problem == "code":
            code = 40002
        elif problem == "boolean_code":
            code = False
        elif problem == "missing_id":
            data = {}
        elif problem == "numeric_id":
            data = {"creative_portfolio_id": 90071992547409938888}
        else:
            data = {
                "creative_portfolio_id": "https://signed.invalid/private?token=synthetic"
            }
        wire.enqueue_create_envelope(
            "CTA",
            {
                "code": code,
                "data": data,
                "request_id": "safe-create",
                "message": "synthetic-secret",
            },
        )
    with pytest.raises(RemoteCallError) as caught:
        adapter.create(
            attempt_id=uuid4(), intent=decode_intent("CTA", build_bodies()["CTA"])
        )
    assert caught.value.effect == "UNKNOWN" and not caught.value.retryable
    assert "synthetic-secret" not in str(caught.value)
    assert len(wire.business_calls()) == 1
    if problem != "lost":
        assert caught.value.evidence.request_id == "safe-create"


def test_unrepresentable_decimal_blocks_before_create(build_case):
    adapter, wire, _ = build_case
    with pytest.raises(RemoteCallError) as caught:
        adapter.create(
            attempt_id=uuid4(),
            intent=decode_intent("ADGROUP", build_bodies()["ADGROUP"]),
        )
    assert caught.value.effect == "NOT_SENT"
    assert wire.business_calls() == []


def test_create_preserves_explicit_non_enable_remote_status(build_case):
    adapter, wire, _ = build_case
    wire.enqueue_created("CAMPAIGN", "remote-campaign", status="DELETED")
    result = adapter.create(
        attempt_id=uuid4(), intent=decode_intent("CAMPAIGN", build_bodies()["CAMPAIGN"])
    )
    assert result.operation_status == "DELETED"
    assert len(wire.business_calls()) == 1
