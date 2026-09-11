"""已核实的公共协议与离线工具合同；未知事实不能成为授权或写入依据。"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from app.core.errors import DomainError

OFFICIAL_ENDPOINT = "https://business-api.tiktok.com/open_mcp/tt-ads-mcp-flat"
OFFICIAL_ISSUER = OFFICIAL_ENDPOINT + "/oauth"
RESOURCE_METADATA_URL = "https://business-api.tiktok.com/.well-known/oauth-protected-resource/open_mcp/tt-ads-mcp-flat"
AUTHORIZATION_METADATA_URL = OFFICIAL_ISSUER + "/.well-known/openid-configuration"
_DIRECTORY = Path(__file__).parent


def require_official_endpoint(value: str) -> str:
    # 精确匹配同时拒绝用户信息、端口、查询参数、尾斜杠和其他服务变体。
    if not isinstance(value, str) or value != OFFICIAL_ENDPOINT:
        raise DomainError("mcp_endpoint_invalid", "MCP 服务配置无效")
    return value


def require_public_official_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "https"
            and parsed.netloc == "business-api.tiktok.com"
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and "\\" not in value
        )
    except TypeError, ValueError:
        valid = False
    if not valid:
        raise DomainError("mcp_protocol_invalid", "MCP 公共协议地址无效")
    return value


@dataclass(frozen=True)
class ToolContract:
    operation: str
    tool_name: str
    effect: Literal["READ", "WRITE"]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    # 此字段约束成功 envelope 内的 data，不表示完整 MCP content 或业务 envelope。
    response_shape: Literal["OBJECT", "OBJECT_LIST"]
    source_urls: tuple[str, ...]
    evidence: Literal["DOCUMENTED", "OBSERVED"] = "DOCUMENTED"
    text_json_envelope: bool = False


@dataclass(frozen=True)
class McpProtocolProfile:
    revision: str
    endpoint: str
    issuer: str | None
    resource: str | None
    authorization_endpoint: str | None
    token_endpoint: str | None
    registration_endpoint: str | None
    revocation_endpoint: str | None
    authorization_evidence: Literal["PUBLIC_METADATA", "UNVERIFIED"]
    sdk_version: str
    httpx_version: str
    sdk_protocol_version: str
    server_protocol_version: str | None
    transport: str
    transport_evidence: str
    pkce_methods: tuple[str, ...] | None
    token_auth_methods: tuple[str, ...] | None
    grant_types: tuple[str, ...] | None
    scopes_supported: tuple[str, ...] | None
    refresh_replay_guaranteed: bool | None
    refresh_semantics_evidence: str
    refresh_semantics_source_urls: tuple[str, ...]
    permission_evidence: dict[str, Any]
    schema_manifest_sha256: str
    source_urls: tuple[str, ...]
    unverified: tuple[str, ...]

    @property
    def automatic_refresh_replay_allowed(self) -> bool:
        return (
            self.refresh_replay_guaranteed is True
            and self.refresh_semantics_evidence == "DOCUMENTED"
            and bool(self.refresh_semantics_source_urls)
        )

    def require_authorization_verified(self) -> None:
        # 只证明公共授权协议字段齐备，不代表租户已经授权、注册或具备业务写权限。
        if not (
            self.authorization_evidence == "PUBLIC_METADATA"
            and self.issuer
            and self.resource
            and self.authorization_endpoint
            and self.token_endpoint
            and self.pkce_methods
            and "S256" in self.pkce_methods
            and self.token_auth_methods
        ):
            raise DomainError("mcp_protocol_unverified", "MCP 授权协议尚未核实")


def parse_profile(raw: dict[str, Any]) -> McpProtocolProfile:
    try:
        values = dict(raw)
        require_official_endpoint(values["endpoint"])
        for key in ("issuer", "resource"):
            if key not in values:
                raise ValueError("missing explicit authorization fact")
        if values["issuer"] not in (None, OFFICIAL_ISSUER):
            raise ValueError("issuer mismatch")
        if values["resource"] not in (None, OFFICIAL_ENDPOINT):
            raise ValueError("resource mismatch")
        for key in (
            "authorization_endpoint",
            "token_endpoint",
            "registration_endpoint",
            "revocation_endpoint",
        ):
            if values[key] is not None:
                require_public_official_url(values[key])
        if values["authorization_evidence"] not in ("PUBLIC_METADATA", "UNVERIFIED"):
            raise ValueError("unknown evidence")
        for key in (
            "pkce_methods",
            "token_auth_methods",
            "grant_types",
            "scopes_supported",
            "refresh_semantics_source_urls",
            "source_urls",
            "unverified",
        ):
            value = values[key]
            if value is not None:
                if not isinstance(value, list) or not all(
                    isinstance(item, str) and item for item in value
                ):
                    raise ValueError("expected string list")
                values[key] = tuple(value)
            elif key in ("refresh_semantics_source_urls", "source_urls", "unverified"):
                raise ValueError("expected explicit source list")
        if (
            values["refresh_replay_guaranteed"] is not None
            and type(values["refresh_replay_guaranteed"]) is not bool
        ):
            raise ValueError("expected explicit replay guarantee")
        if values["refresh_replay_guaranteed"] is not None and not (
            values["refresh_semantics_evidence"] == "DOCUMENTED"
            and values["refresh_semantics_source_urls"]
        ):
            raise ValueError("refresh semantics lack evidence")
        digest = values["schema_manifest_sha256"]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise ValueError("invalid manifest digest")
        profile = McpProtocolProfile(**values)
        if profile.authorization_evidence == "PUBLIC_METADATA":
            profile.require_authorization_verified()
        return profile
    except (KeyError, TypeError, ValueError) as exc:
        raise DomainError("mcp_protocol_invalid", "MCP 协议记录无效") from exc


def load_mcp_protocol() -> McpProtocolProfile:
    try:
        raw = json.loads((_DIRECTORY / "protocol-profile.json").read_text())
        profile = parse_profile(raw)
        digest = hashlib.sha256(
            (_DIRECTORY / "tool-contracts.json").read_bytes()
        ).hexdigest()
        if digest != profile.schema_manifest_sha256:
            raise DomainError("mcp_contract_changed", "MCP 工具合同版本不一致")
        return profile
    except (OSError, ValueError) as exc:
        raise DomainError("mcp_protocol_invalid", "MCP 协议记录不可读") from exc


def load_tool_contracts() -> tuple[ToolContract, ...]:
    load_mcp_protocol()
    raw = json.loads((_DIRECTORY / "tool-contracts.json").read_text())
    return tuple(
        ToolContract(**{**item, "source_urls": tuple(item["source_urls"])})
        for item in raw["contracts"]
    )


# 只移除 JSON Schema 注解；properties/$defs 下同名的业务字段必须保留。
_ANNOTATIONS = frozenset({"description", "title", "examples", "$comment"})
_SCHEMA_MAPS = frozenset(
    {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
)


def _json_identity(value: Any) -> str:
    # JSON 序列化区分 true/1 与 false/0，包含任意深度的业务字面量。
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _semantic_schema(value: Any, *, keyword: str = "") -> Any:
    # const/default/enum 保存业务 JSON，内部键永远不是 schema 注解。
    if keyword in {"const", "default", "enum"}:
        return (
            sorted(value, key=_json_identity)
            if keyword == "enum" and isinstance(value, list)
            else value
        )
    if isinstance(value, dict):
        if keyword in _SCHEMA_MAPS:
            # 此层每个键都是属性/定义名；例如名为 description 的子 schema 必须保留。
            return {name: _semantic_schema(schema) for name, schema in value.items()}
        if keyword in {"dependentRequired", "dependencies"}:
            # dependentRequired 的值为字段名集合；旧版 dependencies 还可包含子 schema。
            return {
                name: sorted(dependency, key=_json_identity)
                if isinstance(dependency, list)
                else _semantic_schema(dependency)
                for name, dependency in value.items()
            }
        return {
            key: _semantic_schema(item, keyword=key)
            for key, item in value.items()
            if key not in _ANNOTATIONS
        }
    if isinstance(value, list):
        items = [_semantic_schema(item) for item in value]
        # required/type 是集合；prefixItems 等有位置语义的数组不能排序。
        return (
            sorted(items, key=_json_identity)
            if keyword in {"required", "type"}
            else items
        )
    return value


def _schema_identity(value: Any) -> str:
    return _json_identity(_semantic_schema(value))


def verify_tool_schema(expected: ToolContract, observed: dict[str, Any]) -> None:
    if (
        not isinstance(observed, dict)
        or observed.get("name") != expected.tool_name
        or not isinstance(observed.get("inputSchema"), dict)
        or _schema_identity(observed["inputSchema"])
        != _schema_identity(expected.input_schema)
        or (
            expected.output_schema is not None
            and _schema_identity(observed.get("outputSchema"))
            != _schema_identity(expected.output_schema)
        )
    ):
        # 不把服务返回的字段或工具说明放入异常，避免传播非可信文本。
        raise DomainError("mcp_contract_changed", "MCP 工具合同已变化")
