"""Synthetic HTTP receipts exercise actual SDK and MCP Client; no live evidence."""

import json
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx2
import pytest
from urllib3.response import HTTPResponse

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.accounts import (
    AuthorizationFacts,
    CandidateReadContext,
    RuntimeReadContext,
)
from app.integrations.tiktok.mcp import transport
from app.integrations.tiktok.mcp.accounts import McpAccountsGateway
from app.integrations.tiktok.mcp.protocol import (
    OFFICIAL_ENDPOINT,
    OFFICIAL_ISSUER,
    load_tool_contracts,
)
from app.integrations.tiktok.mcp.scenes import McpScenesGateway
from app.integrations.tiktok.official.accounts import OfficialAccountsGateway
from app.integrations.tiktok.official.scenes import OfficialScenesGateway
from app.integrations.tiktok.sdk import official_client
from tests.integrations.tiktok.mcp_wire import McpWire

AD = "9007199254740993123"
BC = "9007199254740993124"
AUTH = AuthorizationFacts(
    None,
    None,
    OFFICIAL_ISSUER,
    OFFICIAL_ENDPOINT,
    ("mcp:tt4b",),
    None,
    None,
    None,
    "synthetic token record",
    datetime.now(UTC),
)


def paged(rows, size=50):
    return {
        "list": rows,
        "page_info": {
            "page": 1,
            "page_size": size,
            "total_page": 1,
            "total_number": len(rows),
        },
    }


@pytest.fixture
def channels(monkeypatch):
    contracts = {
        item.operation: item
        for item in load_tool_contracts()
        if item.operation.startswith(("accounts.", "scene."))
    }
    # OBSERVED applies only to this local synthetic wire catalog.
    contracts = {
        key: replace(value, evidence="OBSERVED") for key, value in contracts.items()
    }
    wire = McpWire(
        [
            {"name": item.tool_name, "inputSchema": item.input_schema}
            for item in contracts.values()
        ]
    )
    receipts, calls, events = {}, [], []

    def sdk_request(_pool, method, url, **kwargs):
        assert method == "GET"
        calls.append((url, kwargs))
        assert events[-1][0] == "enter"
        data = receipts[url.split("/v1.3/")[-1]]
        return HTTPResponse(
            body=json.dumps(data).encode(),
            status=200,
            headers={"Content-Type": "application/json"},
        )

    class LocalTransport(httpx2.AsyncBaseTransport):
        def __init__(self):
            self.inner = httpx2.AsyncHTTPTransport(retries=0)

        async def handle_async_request(self, request):
            request.url = httpx2.URL(wire.url)
            return await self.inner.handle_async_request(request)

        async def aclose(self):
            await self.inner.aclose()

    monkeypatch.setattr("urllib3.PoolManager.request", sdk_request)
    monkeypatch.setattr(transport, "_new_http_transport", LocalTransport)

    @contextmanager
    def request_scope(advertiser_id, operation, _deadline):
        events.append(("enter", advertiser_id, operation))
        try:
            yield
        finally:
            events.append(("exit", advertiser_id, operation))

    @contextmanager
    def admit(advertiser_id, operation):
        with request_scope(advertiser_id, operation, None):
            yield

    def queue(path, operation, data, **envelope):
        raw = {"code": 0, "request_id": "upstream", "data": data, **envelope}
        receipts[path] = raw
        wire.results[contracts[operation].tool_name].append(
            {
                "content": [],
                "structuredContent": {
                    **raw,
                    "mcp_request_id": "mcp",
                    "remote_task_id": "task",
                },
            }
        )

    @contextmanager
    def pair(kind, candidate=False, authorization=AUTH, **official_overrides):
        context = CandidateReadContext() if candidate else RuntimeReadContext(BC)
        deadline = datetime.now(UTC) + timedelta(seconds=15)
        with (
            official_client(access_token="synthetic") as sdk_client,
            transport.open_bound_mcp_client(
                token="synthetic",
                task_deadline=deadline,
                authorize=lambda a, o: None,
                admit=admit,
                contracts=contracts,
                observed_tools={item["name"]: item for item in wire.tools},
            ) as mcp_client,
        ):
            if kind == "accounts":
                yield (
                    OfficialAccountsGateway(
                        sdk_client,
                        context=context,
                        authorization=authorization,
                        app_id="synthetic",
                        secret="synthetic",
                        request_scope=request_scope,
                        deadline=deadline,
                        **official_overrides,
                    ),
                    McpAccountsGateway(
                        mcp_client, context=context, authorization=authorization
                    ),
                )
            else:
                yield (
                    OfficialScenesGateway(
                        sdk_client,
                        context=context,
                        request_scope=request_scope,
                        deadline=deadline,
                    ),
                    McpScenesGateway(mcp_client, context=context),
                )

    yield pair, queue, receipts, calls, events, wire
    wire.close()


@pytest.mark.parametrize(
    "resource,path,operation,data,want",
    [
        (
            "account_roles",
            "bc/asset/get/",
            "accounts.list_bc_assets",
            paged(
                [
                    {
                        "asset_id": AD,
                        "asset_type": "ADVERTISER",
                        "advertiser_role": "ADMIN",
                    }
                ]
            ),
            {"matches": [{"advertiser_id": AD, "role": "ADMIN"}]},
        ),
        (
            "identity",
            "identity/get/",
            "scene.list_identities",
            {
                "identity_list": [
                    {
                        "identity_id": "id",
                        "identity_type": "BC_AUTH_TT",
                        "identity_authorized_bc_id": BC,
                        "available_status": "AVAILABLE",
                        "can_push_video": True,
                        "is_gpppa": False,
                    }
                ],
                "page_info": paged([{}])["page_info"],
            },
            {
                "matches": [
                    {
                        "identity_id": "id",
                        "identity_type": "BC_AUTH_TT",
                        "identity_authorized_bc_id": BC,
                    }
                ]
            },
        ),
        (
            "minis",
            "minis/get/",
            "scene.list_minis",
            paged(
                [
                    {
                        "minis_id": "mini",
                        "minis_status": "ACTIVE",
                        "minis_type": "MINI_SERIES",
                        "region_codes": ["US"],
                    }
                ]
            ),
            {
                "matches": [
                    {
                        "minis_id": "mini",
                        "status": "ACTIVE",
                        "type": "MINI_SERIES",
                        "regions": ["US"],
                    }
                ]
            },
        ),
        (
            "cta",
            "creative/cta/recommend/",
            "scene.recommend_ctas",
            {"recommend_assets": [{"asset_ids": ["cta"], "asset_content": "Watch"}]},
            {
                "asset_ids": ["cta"],
                "recommend_assets": [{"asset_ids": ["cta"], "asset_content": "Watch"}],
            },
        ),
        (
            "vbo",
            "tool/vbo_status/",
            "scene.check_vbo",
            {"vo_min_roas": "90071992547409931.123456789"},
            {"vo_min_roas": "90071992547409931.123456789"},
        ),
        (
            "regions",
            "tool/region/",
            "scene.list_regions",
            {
                "region_list": ["US"],
                "region_info": [
                    {
                        "region_code": "US",
                        "location_id": "6252001",
                        "level": "COUNTRY",
                        "area_type": "ADMIN",
                    }
                ],
            },
            {"locations": [{"region_code": "US", "location_id": "6252001"}]},
        ),
    ],
)
def test_scene_channels_have_same_typed_facts_and_exact_values(
    channels, resource, path, operation, data, want
):
    pair, queue, _, calls, events, wire = channels
    queue(path, operation, data)
    with pair("scenes") as adapters:
        pages = [
            adapter.read_page(
                resource=resource, advertiser_id=AD, page=1, minis_id="mini"
            )
            for adapter in adapters
        ]
    assert pages[0].facts == pages[1].facts
    actual = pages[0].facts.model_dump(mode="json", exclude_none=True)
    assert all(actual[key] == value for key, value in want.items())
    assert pages[0].request_id == pages[1].request_id == "upstream"
    assert pages[1].evidence.mcp_request_id == "mcp"
    assert pages[1].evidence.remote_task_id == "task"
    assert len(calls) == 1
    assert len([e for e in events if e[0] == "enter"]) == len(wire.calls) + 1
    if resource in ("identity", "account_roles", "minis"):
        assert actual["seen"] == actual["total_number"] == actual["total_page"] == 1
    if resource == "identity":
        assert dict(calls[0][1]["fields"])["identity_authorized_bc_id"] == BC


def test_account_directory_and_intersection_preserve_authorization_unknown(channels):
    pair, queue, _, calls, events, _ = channels
    queue(
        "bc/get/",
        "accounts.list_bcs",
        paged([{"bc_info": {"bc_id": BC, "name": "BC"}}]),
    )
    queue(
        "bc/asset/get/",
        "accounts.list_bc_assets",
        paged([{"asset_id": AD, "asset_name": "A", "asset_type": "ADVERTISER"}]),
    )
    queue(
        "oauth2/advertiser/get/",
        "accounts.list_authorized_advertisers",
        {"list": [{"advertiser_id": AD}]},
    )
    queue(
        "advertiser/info/",
        "accounts.get_advertisers",
        {
            "list": [
                {
                    "advertiser_id": AD,
                    "name": "A",
                    "currency": "USD",
                    "timezone": "UTC",
                    "status": "ENABLE",
                }
            ]
        },
    )
    with pair("accounts", candidate=True) as adapters:
        bcs = [a.business_centers(page=1, page_size=50) for a in adapters]
        pages = [a.advertisers(bc_id=BC, page=1, page_size=50) for a in adapters]
        assert all(a.authorization_facts() == AUTH for a in adapters)
    assert bcs[0].items == bcs[1].items
    assert bcs[0].items[0].bc_id == BC
    assert pages[0].items == pages[1].items
    row = pages[0].items[0]
    assert row.advertiser_id == AD and row.authorized is True and row.currency == "USD"
    assert len(calls) == 4
    assert (
        len([e for e in events if e[0] == "enter" and not e[2].startswith("protocol.")])
        == 8
    )


def test_runtime_rejects_cross_bc_and_directory_expansion_before_send(channels):
    pair, _, _, calls, _, wire = channels
    with pair("accounts") as adapters:
        for adapter in adapters:
            with pytest.raises(DomainError):
                adapter.advertisers(bc_id="other", page=1, page_size=50)
            with pytest.raises(DomainError):
                adapter.roles(bc_id="other", page=1, page_size=50)
            with pytest.raises(DomainError):
                adapter.business_centers(page=1, page_size=50)
    assert not calls and not [c for c in wire.calls if c["method"] == "tools/call"]


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"list": []},
        paged([{"asset_id": AD}, {"asset_id": AD}]),
        paged([{"asset_id": 9007199254740993123.0}]),
    ],
)
def test_account_schema_failure_never_returns_partial_success(channels, data):
    pair, queue, _, _, _, _ = channels
    queue("bc/asset/get/", "accounts.list_bc_assets", data)
    with pair("accounts") as adapters:
        for adapter in adapters:
            with pytest.raises(DomainError):
                adapter.roles(bc_id=BC, page=1, page_size=50)


def test_roles_keep_unknown_and_do_not_become_write_permission(channels):
    pair, queue, _, _, _, _ = channels
    queue(
        "bc/asset/get/",
        "accounts.list_bc_assets",
        paged([{"asset_id": AD, "asset_type": "ADVERTISER"}]),
    )
    with pair("accounts") as adapters:
        for adapter in adapters:
            row = adapter.roles(bc_id=BC, page=1, page_size=50).items[0]
            assert row.role is None
            assert adapter.authorization_facts().build_authorized is None


def test_mcp_rejects_wrong_issuer_before_using_client(channels):
    pair, _, _, calls, _, _ = channels
    with pytest.raises(DomainError):
        with pair(
            "accounts", authorization=replace(AUTH, issuer="https://evil.invalid")
        ):
            pass
    assert not calls


@pytest.mark.parametrize("kind", ["business", "text"])
def test_mcp_business_error_is_rejected_and_native_text_json_is_accepted(
    channels, kind
):
    pair, queue, _, _, _, wire = channels
    queue(
        "tool/vbo_status/",
        "scene.check_vbo",
        {"vo_status": "ON"},
        code=40001 if kind == "business" else 0,
    )
    if kind == "text":
        item = wire.results[
            next(
                c.tool_name
                for c in load_tool_contracts()
                if c.operation == "scene.check_vbo"
            )
        ].popleft()
        wire.results[
            next(
                c.tool_name
                for c in load_tool_contracts()
                if c.operation == "scene.check_vbo"
            )
        ].append(
            {
                "content": [
                    {"type": "text", "text": json.dumps(item["structuredContent"])}
                ]
            }
        )
    with pair("scenes") as (_, adapter):
        if kind == "business":
            with pytest.raises(DomainError):
                adapter.read_page(
                    resource="vbo", advertiser_id=AD, page=1, minis_id=None
                )
        else:
            assert adapter.read_page(
                resource="vbo", advertiser_id=AD, page=1, minis_id=None
            )


@pytest.mark.parametrize(
    "data",
    [
        {"vo_min_roas": 1.25},
        {"identity_list": [], "page_info": {"page": 1}},
        {
            "identity_list": [{"identity_id": "a"}, {"identity_id": "a"}],
            "page_info": {
                "page": 1,
                "page_size": 50,
                "total_page": 1,
                "total_number": 2,
            },
        },
    ],
)
def test_scene_channels_reject_malformed_facts(channels, data):
    pair, queue, _, _, _, _ = channels
    resource = "vbo" if "vo_min_roas" in data else "identity"
    queue(
        "tool/vbo_status/" if resource == "vbo" else "identity/get/",
        "scene.check_vbo" if resource == "vbo" else "scene.list_identities",
        data,
    )
    with pair("scenes") as adapters:
        for adapter in adapters:
            with pytest.raises(DomainError):
                adapter.read_page(
                    resource=resource, advertiser_id=AD, page=1, minis_id=None
                )


def test_official_business_failure_is_sanitized(channels):
    pair, queue, _, calls, _, _ = channels
    queue(
        "tool/vbo_status/",
        "scene.check_vbo",
        {},
        code=40001,
        message="synthetic-sensitive",
    )
    with pair("scenes") as (adapter, _):
        with pytest.raises(DomainError) as exc:
            adapter.read_page(resource="vbo", advertiser_id=AD, page=1, minis_id=None)
    assert len(calls) == 1
    assert "synthetic-sensitive" not in str(exc.value)


def test_official_rechecks_deadline_after_admission_without_sending(channels):
    from time import sleep

    _, _, _, calls, _, _ = channels

    @contextmanager
    def expired_scope(_advertiser_id, _operation, _deadline):
        sleep(0.025)
        yield

    with official_client(access_token="synthetic") as client:
        adapter = OfficialScenesGateway(
            client,
            context=RuntimeReadContext(BC),
            request_scope=expired_scope,
            deadline=datetime.now(UTC) + timedelta(milliseconds=10),
        )
        with pytest.raises(DomainError) as exc:
            adapter.read_page(resource="vbo", advertiser_id=AD, page=1, minis_id=None)
        assert exc.value.code == "read_deadline_exceeded"
    assert not calls


def test_account_details_intersect_assets_and_authorization_before_request(channels):
    pair, queue, _, calls, _, wire = channels
    queue(
        "bc/asset/get/",
        "accounts.list_bc_assets",
        paged(
            [
                {"asset_id": AD, "asset_type": "ADVERTISER"},
                {"asset_id": "unauthorized", "asset_type": "ADVERTISER"},
            ]
        ),
    )
    queue(
        "oauth2/advertiser/get/",
        "accounts.list_authorized_advertisers",
        {"list": [{"advertiser_id": AD}, {"advertiser_id": "outside-bc"}]},
    )
    queue(
        "advertiser/info/",
        "accounts.get_advertisers",
        {"list": [{"advertiser_id": AD}]},
    )
    with pair("accounts") as adapters:
        pages = [
            adapter.advertisers(bc_id=BC, page=1, page_size=50) for adapter in adapters
        ]
    assert pages[0].items == pages[1].items
    assert pages[0].total_number == 2
    assert pages[0].items[1].authorized is False
    fields = dict(
        next(
            options["fields"]
            for url, options in calls
            if url.endswith("/advertiser/info/")
        )
    )
    assert json.loads(fields["advertiser_ids"]) == [AD]
    detail_tool = next(
        c.tool_name
        for c in load_tool_contracts()
        if c.operation == "accounts.get_advertisers"
    )
    args = next(
        c["params"]["arguments"]
        for c in wire.calls
        if c["method"] == "tools/call" and c["params"]["name"] == detail_tool
    )
    assert args["advertiser_ids"] == [AD]


def test_official_multirequest_page_cannot_reuse_first_admission(channels):
    _, queue, _, calls, events, _ = channels
    queue(
        "bc/asset/get/",
        "accounts.list_bc_assets",
        paged([{"asset_id": AD, "asset_type": "ADVERTISER"}]),
    )

    @contextmanager
    def scope(_advertiser_id, operation, _deadline):
        if operation == "accounts.list_authorized_advertisers":
            raise DomainError("connection_unavailable", "synthetic revocation")
        events.append(("enter", None, operation))
        yield

    with official_client(access_token="synthetic") as client:
        adapter = OfficialAccountsGateway(
            client,
            context=RuntimeReadContext(BC),
            authorization=AUTH,
            app_id="synthetic",
            secret="synthetic",
            request_scope=scope,
            deadline=datetime.now(UTC) + timedelta(seconds=5),
        )
        with pytest.raises(DomainError) as exc:
            adapter.advertisers(bc_id=BC, page=1, page_size=50)
    assert exc.value.code == "connection_unavailable"
    assert len(calls) == 1 and calls[0][0].endswith("/bc/asset/get/")


def test_candidates_cannot_create_scene_reader(channels):
    pair, _, _, calls, _, _ = channels
    with pytest.raises(DomainError):
        with pair("scenes", candidate=True):
            pass
    assert not calls


@pytest.mark.parametrize("code", [False, 0.5, "0"])
@pytest.mark.parametrize("kind", ["accounts", "scenes"])
def test_channels_reject_raw_business_code_before_sdk_integer_coercion(
    channels, code, kind
):
    pair, queue, _, calls, _, _ = channels
    if kind == "scenes":
        queue("tool/vbo_status/", "scene.check_vbo", {"vo_status": "ON"}, code=code)
    else:
        queue(
            "bc/asset/get/",
            "accounts.list_bc_assets",
            paged([{"asset_id": AD, "asset_type": "ADVERTISER"}]),
            code=code,
        )
    with pair(kind) as adapters:
        for adapter in adapters:
            with pytest.raises(DomainError):
                if kind == "scenes":
                    adapter.read_page(
                        resource="vbo", advertiser_id=AD, page=1, minis_id=None
                    )
                else:
                    adapter.roles(bc_id=BC, page=1, page_size=50)
    assert len(calls) == 1


@pytest.mark.parametrize("code", [0, 40001])
def test_official_raw_envelope_keeps_valid_zero_and_nonzero_business_error(
    channels, code
):
    pair, queue, _, calls, _, _ = channels
    queue(
        "bc/asset/get/",
        "accounts.list_bc_assets",
        paged([{"asset_id": AD, "asset_type": "ADVERTISER"}]),
        code=code,
        message="synthetic-sensitive",
    )
    with pair("accounts") as (adapter, _):
        if code == 0:
            result = adapter.roles(bc_id=BC, page=1, page_size=50)
            assert result.items[0].advertiser_id == AD
        else:
            with pytest.raises(DomainError) as exc:
                adapter.roles(bc_id=BC, page=1, page_size=50)
            assert exc.value.code == "tiktok_response_error"
            assert "synthetic-sensitive" not in str(exc.value)
    assert len(calls) == 1
