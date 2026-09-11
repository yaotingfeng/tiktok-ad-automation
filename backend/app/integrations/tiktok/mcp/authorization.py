"""当前 MCP 材料的独立观察输入；不冒充已完整核实的运行授权事实。"""

import json
from datetime import datetime

from app.integrations.tiktok.contracts.accounts import AuthorizationFacts
from app.integrations.tiktok.mcp.protocol import load_mcp_protocol


def material_authorization(
    material: dict[str, str], *, observed_at: datetime
) -> AuthorizationFacts:
    from app.integrations.tiktok.mcp_auth.service import load_registration

    profile = load_mcp_protocol()
    assert profile.issuer is not None and profile.resource is not None
    scopes: tuple[str, ...] = ()
    source = "UNKNOWN"
    try:
        values = json.loads(material["scopes"])
        if (
            type(values) is not list
            or len(values) > 1024
            or any(type(v) is not str or not v.strip() or len(v) > 255 for v in values)
            or len(set(values)) != len(values)
            or material.get("issuer") != profile.issuer
            or material.get("resource") != profile.resource
            or material.get("client_id") != load_registration(profile).client_id
        ):
            raise ValueError("unverified material")
        scopes = tuple(sorted(values))
        source = "MCP_TOKEN_SCOPE_WITH_CLIENT"
    except KeyError, ValueError, TypeError:
        pass
    return AuthorizationFacts(
        subject_id=None,
        grant_id=None,
        issuer=profile.issuer,
        resource=profile.resource,
        scopes=scopes,
        read_authorized=None,
        upload_authorized=None,
        build_authorized=None,
        evidence_source=source,
        observed_at=observed_at,
    )
