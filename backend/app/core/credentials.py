"""Authenticated tenant-bound envelopes, shared by external integrations."""

import json
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings
from app.core.errors import DomainError


def encrypt_credentials(*, tenant_id: UUID, value: dict[str, str]) -> str:
    settings.require_connection_encryption()
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise DomainError("credential_invalid", "凭据格式无效")
    body = json.dumps({"tenant_id": str(tenant_id), "value": value}).encode()
    return Fernet(settings.CONNECTION_ENCRYPTION_KEY.encode()).encrypt(body).decode()


def decrypt_credentials(*, tenant_id: UUID, ciphertext: str) -> dict[str, str]:
    settings.require_connection_encryption()
    try:
        data = json.loads(
            Fernet(settings.CONNECTION_ENCRYPTION_KEY.encode()).decrypt(
                ciphertext.encode()
            )
        )
    except InvalidToken, ValueError, TypeError, AttributeError:
        raise DomainError("credential_invalid", "凭据无法解密") from None
    if not isinstance(data, dict):
        raise DomainError("credential_invalid", "凭据格式无效")
    if data.get("tenant_id") != str(tenant_id):
        raise DomainError("credential_tenant_mismatch", "凭据不属于当前租户")
    value = data.get("value")
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise DomainError("credential_invalid", "凭据格式无效")
    return value
