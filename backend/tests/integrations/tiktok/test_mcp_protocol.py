import copy
import json
from pathlib import Path

import pytest

from app.core.errors import DomainError
from app.integrations.tiktok.mcp.protocol import (
    OFFICIAL_ENDPOINT,
    ToolContract,
    load_mcp_protocol,
    load_tool_contracts,
    parse_profile,
    require_official_endpoint,
    verify_tool_schema,
)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://business-api.tiktok.com.evil.example/mcp",
        "https://user:password@business-api.tiktok.com/open_mcp/tt-ads-mcp-flat",
        OFFICIAL_ENDPOINT + "?redirect=evil",
        OFFICIAL_ENDPOINT + "/",
        OFFICIAL_ENDPOINT.replace("https:", "http:"),
        "https://business-api.tiktok.com/open_mcp/tt-ads-mcp-layer",
        "https://[invalid",
        None,
    ],
)
def test_only_exact_official_endpoint_is_allowed(endpoint):
    with pytest.raises(DomainError) as exc:
        require_official_endpoint(endpoint)
    assert exc.value.code == "mcp_endpoint_invalid"


def raw_profile():
    return json.loads(
        (
            Path(__file__).parents[3]
            / "app/integrations/tiktok/mcp/protocol-profile.json"
        ).read_text()
    )


@pytest.mark.parametrize("field", ["issuer", "resource"])
def test_profile_requires_explicit_authorization_facts(field):
    raw = raw_profile()
    del raw[field]
    with pytest.raises(DomainError) as exc:
        parse_profile(raw)
    assert exc.value.code == "mcp_protocol_invalid"


def test_unverified_refresh_cannot_imply_replay_guarantee():
    profile = load_mcp_protocol()
    assert profile.refresh_replay_guaranteed is None
    assert profile.automatic_refresh_replay_allowed is False
    raw = raw_profile()
    raw["refresh_replay_guaranteed"] = True
    with pytest.raises(DomainError):
        parse_profile(raw)


def contract():
    return ToolContract(
        operation="video.upload",
        tool_name="file_video_ad_upload",
        effect="WRITE",
        input_schema={
            "type": "object",
            "properties": {
                "advertiser_id": {"type": "string"},
                "payload": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                        "mode": {"type": "string", "enum": ["URL", "ID"]},
                    },
                    "required": ["mode"],
                },
            },
            "required": ["advertiser_id", "payload"],
        },
        output_schema={
            "type": "object",
            "required": ["code"],
            "properties": {"code": {"type": "integer"}},
        },
        response_shape="OBJECT",
        source_urls=("https://business-api.tiktok.com/",),
    )


def observed(expected):
    return {
        "name": expected.tool_name,
        "inputSchema": copy.deepcopy(expected.input_schema),
        "outputSchema": copy.deepcopy(expected.output_schema),
    }


@pytest.mark.parametrize(
    "change",
    ["required", "type", "enum", "nested", "output", "name", "description_property"],
)
def test_contract_rejects_semantic_drift(change):
    expected = contract()
    actual = observed(expected)
    schema = actual["inputSchema"]
    if change == "required":
        schema["required"].remove("advertiser_id")
    elif change == "type":
        schema["properties"]["advertiser_id"]["type"] = "number"
    elif change == "enum":
        schema["properties"]["payload"]["properties"]["mode"]["enum"].remove("ID")
    elif change == "nested":
        del schema["properties"]["payload"]["properties"]["mode"]
    elif change == "output":
        del actual["outputSchema"]
    elif change == "name":
        actual["name"] = "different_tool"
    else:
        schema["properties"]["payload"]["properties"]["description"]["type"] = "number"
    with pytest.raises(DomainError) as exc:
        verify_tool_schema(expected, actual)
    assert exc.value.code == "mcp_contract_changed"


def test_contract_ignores_prose_and_set_order_only():
    expected = contract()
    actual = observed(expected)
    actual["inputSchema"]["description"] = "updated docs"
    actual["inputSchema"]["required"].reverse()
    actual["inputSchema"]["properties"]["payload"]["properties"]["mode"][
        "enum"
    ].reverse()
    verify_tool_schema(expected, actual)


def test_packaged_manifest_is_documented_and_fail_closed():
    profile = load_mcp_protocol()
    contracts = load_tool_contracts()
    assert len(contracts) >= 25
    assert len({item.operation for item in contracts}) == len(contracts)
    assert len(profile.schema_manifest_sha256) == 64
    for item in contracts:
        assert item.evidence == "DOCUMENTED"
        assert item.text_json_envelope == item.operation.startswith("accounts.")
        assert item.output_schema is None
        verify_tool_schema(
            item, {"name": item.tool_name, "inputSchema": item.input_schema}
        )
    video = next(item for item in contracts if item.tool_name == "file_video_ad_upload")
    assert "advertiser_id" in video.input_schema["required"]
    assert video.input_schema["properties"]["auto_bind_enabled"]["type"] == "boolean"
    assert "video_signature" not in video.input_schema["properties"]


def test_profile_rejects_unsafe_authorization_endpoint():
    raw = raw_profile()
    raw["authorization_endpoint"] = "https://evil.example/authorize"
    with pytest.raises(DomainError):
        parse_profile(raw)


def test_literal_json_values_named_description_are_semantic():
    expected = contract()
    expected.input_schema["properties"]["payload"]["const"] = {
        "description": "original"
    }
    actual = observed(expected)
    actual["inputSchema"]["properties"]["payload"]["const"]["description"] = "changed"
    with pytest.raises(DomainError) as exc:
        verify_tool_schema(expected, actual)
    assert exc.value.code == "mcp_contract_changed"


def test_metadata_probe_never_follows_redirect_or_sends_credentials():
    import asyncio

    import httpx2

    from app.integrations.tiktok.mcp.protocol import RESOURCE_METADATA_URL
    from scripts.inspect_tiktok_mcp_protocol import fetch_public_metadata

    requests = []

    def handle(request):
        requests.append(request)
        return httpx2.Response(302, headers={"Location": "https://evil.example/steal"})

    result = asyncio.run(
        fetch_public_metadata(
            RESOURCE_METADATA_URL, transport=httpx2.MockTransport(handle)
        )
    )
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert "authorization" not in requests[0].headers
    assert "cookie" not in requests[0].headers
    assert result["evidence"] == "UNVERIFIED"
    assert "evil.example" not in json.dumps(result)


def test_metadata_probe_exports_only_public_fields():
    import asyncio

    import httpx2

    from app.integrations.tiktok.mcp.protocol import (
        OFFICIAL_ISSUER,
        RESOURCE_METADATA_URL,
    )
    from scripts.inspect_tiktok_mcp_protocol import fetch_public_metadata

    def handle(_request):
        return httpx2.Response(
            200,
            json={
                "resource": OFFICIAL_ENDPOINT,
                "authorization_servers": [OFFICIAL_ISSUER],
                "scopes_supported": ["mcp:tt4b"],
                "access_token": "synthetic-secret",
                "client_secret": "synthetic-secret",
                "accounts": ["synthetic-account"],
            },
        )

    result = asyncio.run(
        fetch_public_metadata(
            RESOURCE_METADATA_URL, transport=httpx2.MockTransport(handle)
        )
    )
    assert result["evidence"] == "PUBLIC_METADATA"
    assert "synthetic" not in json.dumps(result)


def test_metadata_probe_rejects_unpinned_url_before_io():
    import asyncio

    from scripts.inspect_tiktok_mcp_protocol import fetch_public_metadata

    with pytest.raises(ValueError):
        asyncio.run(fetch_public_metadata("https://evil.example/metadata"))


def test_manifest_edit_requires_matching_reviewed_digest(tmp_path, monkeypatch):
    from app.integrations.tiktok.mcp import protocol

    source = Path(protocol.__file__).parent
    (tmp_path / "protocol-profile.json").write_bytes(
        (source / "protocol-profile.json").read_bytes()
    )
    (tmp_path / "tool-contracts.json").write_bytes(
        (source / "tool-contracts.json").read_bytes() + b" "
    )
    monkeypatch.setattr(protocol, "_DIRECTORY", tmp_path)
    with pytest.raises(DomainError) as exc:
        load_tool_contracts()
    assert exc.value.code == "mcp_contract_changed"


def test_unknown_authorization_requires_explicit_unverified_status():
    raw = raw_profile()
    raw["issuer"] = None
    raw["resource"] = None
    with pytest.raises(DomainError):
        parse_profile(raw)
    raw["authorization_evidence"] = "UNVERIFIED"
    profile = parse_profile(raw)
    with pytest.raises(DomainError) as exc:
        profile.require_authorization_verified()
    assert exc.value.code == "mcp_protocol_unverified"


def test_image_search_displayable_matches_documented_boolean_declaration():
    # 已提供官方声明是 displayable?: true | false；独立于 manifest 构造预期值。
    image_search = next(
        item
        for item in load_tool_contracts()
        if item.tool_name == "file_image_ad_search"
    )
    schema = image_search.input_schema["properties"]["filtering"]["properties"][
        "displayable"
    ]
    assert schema["type"] == "boolean"
    assert {json.dumps(value) for value in schema["enum"]} == {"true", "false"}


@pytest.mark.parametrize(
    ("keyword", "before", "after"),
    [
        ("const", True, 1),
        ("const", False, 0),
        ("enum", [False], [0]),
        ("enum", [True], [1]),
        (
            "const",
            {"description": [True, {"flag": False}]},
            {"description": [1, {"flag": 0}]},
        ),
        ("enum", [{"description": [False]}], [{"description": [0]}]),
    ],
)
def test_literal_json_comparison_preserves_boolean_number_types(keyword, before, after):
    expected = contract()
    expected.input_schema[keyword] = before
    actual = observed(expected)
    actual["inputSchema"][keyword] = after
    with pytest.raises(DomainError) as exc:
        verify_tool_schema(expected, actual)
    assert exc.value.code == "mcp_contract_changed"


@pytest.mark.parametrize(
    ("keyword", "before", "after"),
    [
        ("dependentRequired", ["a"], ["b"]),
        ("dependencies", ["a"], ["b"]),
        ("dependencies", {"type": "string"}, {"type": "number"}),
        ("dependencies", {"required": ["a"]}, {"required": ["b"]}),
    ],
)
def test_dependency_maps_preserve_business_keys_named_description(
    keyword, before, after
):
    expected = contract()
    expected.input_schema[keyword] = {"description": before}
    actual = observed(expected)
    actual["inputSchema"][keyword]["description"] = after
    with pytest.raises(DomainError) as exc:
        verify_tool_schema(expected, actual)
    assert exc.value.code == "mcp_contract_changed"


def test_dependency_maps_ignore_only_schema_prose_and_required_order():
    expected = contract()
    expected.input_schema["dependentRequired"] = {"description": ["a", "b"]}
    expected.input_schema["dependencies"] = {
        "description": {
            "type": "object",
            "required": ["a", "b"],
            "description": "old prose",
        },
        "title": ["a", "b"],
    }
    actual = observed(expected)
    actual["inputSchema"]["dependentRequired"]["description"].reverse()
    actual["inputSchema"]["dependencies"]["title"].reverse()
    actual["inputSchema"]["dependencies"]["description"]["required"].reverse()
    actual["inputSchema"]["dependencies"]["description"]["description"] = "new prose"
    verify_tool_schema(expected, actual)


@pytest.mark.parametrize(
    "tool_name",
    [
        "auth_advertiser_get",
        "user_info_get",
        "bc_get",
        "bc_asset_get",
        "bc_member_get",
        "bc_asset_member_get",
    ],
)
def test_live_account_schema_accepts_equivalent_object_and_numeric_annotations(
    tool_name,
):
    expected = next(c for c in load_tool_contracts() if c.tool_name == tool_name)
    actual = observed(expected)
    schema = actual["inputSchema"]
    if schema.get("properties") == {}:
        del schema["properties"]
    for key in ("page", "page_size"):
        if key in schema.get("properties", {}):
            schema["properties"][key]["format"] = "double"
    verify_tool_schema(expected, actual)


@pytest.mark.parametrize(
    "change",
    [
        "numeric_type",
        "minimum",
        "string_format",
        "literal_format",
        "business_properties",
    ],
)
def test_schema_equivalence_preserves_constraints_and_literal_keys(change):
    expected = contract()
    expected.input_schema["properties"]["page"] = {"type": "number"}
    expected.input_schema["properties"]["payload"]["const"] = {
        "format": "double",
        "properties": {},
    }
    expected.input_schema["properties"]["properties"] = {}
    actual = observed(expected)
    if change == "numeric_type":
        actual["inputSchema"]["properties"]["page"]["type"] = "integer"
    elif change == "minimum":
        actual["inputSchema"]["properties"]["page"]["minimum"] = 1
    elif change == "string_format":
        actual["inputSchema"]["properties"]["advertiser_id"]["format"] = "uuid"
    elif change == "literal_format":
        del actual["inputSchema"]["properties"]["payload"]["const"]["format"]
    else:
        del actual["inputSchema"]["properties"]["properties"]
    with pytest.raises(DomainError):
        verify_tool_schema(expected, actual)
