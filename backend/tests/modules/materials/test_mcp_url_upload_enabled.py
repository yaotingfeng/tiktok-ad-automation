"""已接入的 MCP 使用实际上传回读，不依赖永远未知的服务内部保证。"""

import pytest

from app.core.config import settings
from app.core.errors import DomainError
from app.integrations.tiktok.material_upload_evidence import material_upload_policy
from app.integrations.tiktok.mcp.protocol import load_mcp_protocol
from app.modules.materials.channel_policy import require_url_upload


def test_current_mcp_contract_accepts_uploaded_original():
    policy = material_upload_policy(
        channel="OFFICIAL_MCP",
        adapter_contract_revision=load_mcp_protocol().schema_manifest_sha256,
    )
    require_url_upload(policy, byte_size=22585602)


def test_current_mcp_contract_still_enforces_application_limit(monkeypatch):
    monkeypatch.setattr(settings, "MATERIAL_URL_MAX_UPLOAD_BYTES", 1024)
    policy = material_upload_policy(
        channel="OFFICIAL_MCP",
        adapter_contract_revision=load_mcp_protocol().schema_manifest_sha256,
    )
    with pytest.raises(DomainError) as error:
        require_url_upload(policy, byte_size=1025)
    assert error.value.code == "material_channel_capacity"
