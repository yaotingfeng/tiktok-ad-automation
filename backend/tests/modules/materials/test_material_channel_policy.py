"""上传先核实通道合同，不以应用容量或宽松真值代替远端能力证明。"""

from dataclasses import FrozenInstanceError

import pytest

from app.core.config import settings
from app.core.errors import DomainError
from app.modules.materials.channel_policy import (
    MaterialUploadPolicy,
    require_url_upload,
)


@pytest.mark.parametrize(
    "policy",
    [
        MaterialUploadPolicy(None, True, True, True, False),
        MaterialUploadPolicy(1024**3, False, True, True, False),
        MaterialUploadPolicy(1024**3, True, False, True, False),
        MaterialUploadPolicy(1024**3, True, True, False, False),
        MaterialUploadPolicy(1024**3, True, True, True, True),
        MaterialUploadPolicy(1024**3, 1, True, True, False),
        MaterialUploadPolicy(1024**3, True, True, True, None),
        MaterialUploadPolicy(True, True, True, True, False),
        MaterialUploadPolicy(0, True, True, True, False),
    ],
)
def test_unknown_contract_does_not_enable_upload(policy):
    with pytest.raises(DomainError) as error:
        require_url_upload(policy, byte_size=1)
    assert error.value.code == "material_channel_unverified"


def test_application_capacity_does_not_override_service_evidence(monkeypatch):
    monkeypatch.setattr(settings, "MATERIAL_URL_MAX_UPLOAD_BYTES", 1024**3)
    policy = MaterialUploadPolicy(256 * 1024**2, True, True, True, False)
    require_url_upload(policy, byte_size=256 * 1024**2)
    with pytest.raises(DomainError) as error:
        require_url_upload(policy, byte_size=1024**3)
    assert error.value.code == "material_channel_capacity"


def test_service_capacity_does_not_override_application_limit(monkeypatch):
    monkeypatch.setattr(settings, "MATERIAL_URL_MAX_UPLOAD_BYTES", 16)
    policy = MaterialUploadPolicy(1024, True, True, True, False)
    require_url_upload(policy, byte_size=16)
    with pytest.raises(DomainError) as error:
        require_url_upload(policy, byte_size=17)
    assert error.value.code == "material_channel_capacity"


@pytest.mark.parametrize("byte_size", [True, False, None, "1", 1.0, 0, -1])
def test_size_must_be_a_positive_actual_integer(byte_size):
    with pytest.raises(DomainError) as error:
        require_url_upload(
            MaterialUploadPolicy(1024, True, True, True, False), byte_size=byte_size
        )
    assert error.value.code == "material_channel_capacity"


def test_policy_cannot_be_mutated_after_selection():
    policy = MaterialUploadPolicy(None, False, False, False, True)
    with pytest.raises(FrozenInstanceError):
        policy.max_bytes = 1024


def test_production_mcp_upload_remains_unverified_and_revision_bound():
    from app.integrations.tiktok.material_upload_evidence import material_upload_policy
    from app.integrations.tiktok.mcp.protocol import load_mcp_protocol

    for revision in (load_mcp_protocol().schema_manifest_sha256, "another-contract"):
        policy = material_upload_policy(
            channel="OFFICIAL_MCP", adapter_contract_revision=revision
        )
        with pytest.raises(DomainError) as error:
            require_url_upload(policy, byte_size=1)
        assert error.value.code == "material_channel_unverified"


def test_synthetic_evidence_is_bound_to_exact_channel_and_contract(monkeypatch):
    from app.integrations.tiktok import material_upload_evidence as evidence

    record = evidence.MaterialUploadEvidence(
        channel="OFFICIAL_MCP",
        adapter_contract_revision="synthetic-contract-1",
        policy=MaterialUploadPolicy(100, True, True, True, False),
        category="SYNTHETIC",
        sources=("offline HTTP fixture",),
        notes="Only a local protocol/state-machine proof.",
    )
    monkeypatch.setattr(evidence, "UPLOAD_EVIDENCE", (record,))
    policy = evidence.material_upload_policy(
        channel="OFFICIAL_MCP", adapter_contract_revision="synthetic-contract-1"
    )
    require_url_upload(policy, byte_size=100)
    for channel, revision in [
        ("OFFICIAL_API", "synthetic-contract-1"),
        ("OFFICIAL_MCP", "synthetic-contract-2"),
    ]:
        with pytest.raises(DomainError):
            require_url_upload(
                evidence.material_upload_policy(
                    channel=channel, adapter_contract_revision=revision
                ),
                byte_size=1,
            )
