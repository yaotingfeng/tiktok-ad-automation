import json
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

from app.core.config import settings
from app.core.credentials import decrypt_credentials, encrypt_credentials
from app.core.errors import DomainError


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    monkeypatch.setattr(
        settings, "CONNECTION_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )


def test_credentials_are_authenticated_and_tenant_bound():
    tenant_id = uuid4()
    value = {"access_token": "fake-secret", "provider_password": "中文"}
    encrypted = encrypt_credentials(tenant_id=tenant_id, value=value)
    assert "fake-secret" not in encrypted
    assert decrypt_credentials(tenant_id=tenant_id, ciphertext=encrypted) == value
    with pytest.raises(DomainError) as error:
        decrypt_credentials(tenant_id=uuid4(), ciphertext=encrypted)
    assert error.value.code == "credential_tenant_mismatch"


@pytest.mark.parametrize(
    "body",
    [
        [],
        {},
        {"tenant_id": "owner", "value": []},
        {"tenant_id": "owner", "value": {"token": 1}},
    ],
)
def test_malformed_encrypted_envelopes_are_rejected(body):
    tenant_id = uuid4()
    if isinstance(body, dict) and "tenant_id" in body:
        body["tenant_id"] = str(tenant_id)
    ciphertext = (
        Fernet(settings.CONNECTION_ENCRYPTION_KEY.encode())
        .encrypt(json.dumps(body).encode())
        .decode()
    )
    with pytest.raises(DomainError) as error:
        decrypt_credentials(tenant_id=tenant_id, ciphertext=ciphertext)
    assert error.value.code in {"credential_invalid", "credential_tenant_mismatch"}


def test_invalid_ciphertext_never_echoes_input():
    with pytest.raises(DomainError) as error:
        decrypt_credentials(tenant_id=uuid4(), ciphertext="fake-secret")
    assert error.value.code == "credential_invalid"
    assert "fake-secret" not in str(error.value)
    assert error.value.__cause__ is None


def test_missing_encryption_key_fails_closed(monkeypatch):
    monkeypatch.setattr(settings, "CONNECTION_ENCRYPTION_KEY", "")
    with pytest.raises(DomainError) as error:
        encrypt_credentials(tenant_id=uuid4(), value={"token": "fake-secret"})
    assert error.value.code == "connection_encryption_unconfigured"
