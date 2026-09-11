"""两种通道独立准备状态；公开结果不包含部署字段或凭据值。"""

from typing import Literal

from app.core.config import settings
from app.core.errors import DomainError
from app.integrations.tiktok.auth import _configured_authorization_url
from app.integrations.tiktok.mcp.protocol import load_mcp_protocol
from app.integrations.tiktok.mcp_auth.service import load_registration
from app.modules.accounts.schemas import (
    AppConfiguration,
    ChannelConfiguration,
    McpConfiguration,
)


def mcp_configuration() -> McpConfiguration:
    try:
        settings.require_connection_encryption()
        load_registration(load_mcp_protocol())
    except DomainError as error:
        status: Literal["INVALID", "CLIENT_UNREGISTERED", "PROTOCOL_UNVERIFIED"] = "INVALID"
        if error.code == "mcp_client_unregistered":
            status = "CLIENT_UNREGISTERED"
        elif error.code == "mcp_protocol_unverified":
            status = "PROTOCOL_UNVERIFIED"
        return McpConfiguration(configured=False, code=error.code, status=status)
    return McpConfiguration(configured=True, status="READY")


def channel_configuration() -> AppConfiguration:
    try:
        _configured_authorization_url()
    except DomainError as error:
        api = ChannelConfiguration(
            kind="OFFICIAL_API",
            configured=False,
            code=error.code,
            status="NOT_CONFIGURED"
            if len(settings.tiktok_app_missing_fields) == 3
            else "INCOMPLETE",
        )
    else:
        api = ChannelConfiguration(kind="OFFICIAL_API", configured=True, status="READY")
    mcp = mcp_configuration()
    return AppConfiguration(
        channels=(
            api,
            ChannelConfiguration(
                kind="OFFICIAL_MCP",
                configured=mcp.configured,
                status=mcp.status,
                code=mcp.code,
            ),
        )
    )
