"""工程容量与接口版本检查，不伪造服务能力事实。"""

from dataclasses import FrozenInstanceError

import pytest

from app.core.config import settings
from app.core.errors import DomainError
from app.modules.materials.channel_policy import (
    MaterialUploadPolicy,
    require_url_upload,
)


@pytest.mark.parametrize("limit", [None, True, 0, -1, "1024"])
def test_unavailable_contract_does_not_enable_upload(limit):
    with pytest.raises(DomainError) as error:
        require_url_upload(MaterialUploadPolicy(limit), byte_size=1)
    assert error.value.code == "material_channel_unverified"


def test_application_and_adapter_limits_both_apply(monkeypatch):
    monkeypatch.setattr(settings, "MATERIAL_URL_MAX_UPLOAD_BYTES", 16)
    require_url_upload(MaterialUploadPolicy(1024), byte_size=16)
    for policy, size in [
        (MaterialUploadPolicy(1024), 17),
        (MaterialUploadPolicy(8), 9),
    ]:
        with pytest.raises(DomainError) as error:
            require_url_upload(policy, byte_size=size)
        assert error.value.code == "material_channel_capacity"


@pytest.mark.parametrize("byte_size", [True, False, None, "1", 1.0, 0, -1])
def test_size_must_be_a_positive_actual_integer(byte_size):
    with pytest.raises(DomainError) as error:
        require_url_upload(MaterialUploadPolicy(1024), byte_size=byte_size)
    assert error.value.code == "material_channel_capacity"


def test_policy_cannot_be_mutated_after_selection():
    policy = MaterialUploadPolicy(None)
    with pytest.raises(FrozenInstanceError):
        policy.max_bytes = 1024


def test_unrecognized_channel_or_revision_cannot_send():
    from app.integrations.tiktok.material_upload_evidence import material_upload_policy

    for channel, revision in [
        ("OFFICIAL_MCP", "another-contract"),
        ("OTHER", "official-api-v1"),
    ]:
        with pytest.raises(DomainError) as error:
            require_url_upload(
                material_upload_policy(
                    channel=channel, adapter_contract_revision=revision
                ),
                byte_size=1,
            )
        assert error.value.code == "material_channel_unverified"
