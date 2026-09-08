from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

ConnectionStatus = Literal[
    "PENDING_AUTH", "DISCOVERING", "ACTIVE", "REAUTH_REQUIRED", "ERROR", "DISABLED"
]


class ConnectionPublic(BaseModel):
    """Explicit allowlist: never serialize ORM credential fields to HTTP."""

    model_config = ConfigDict(from_attributes=True)
    id: UUID
    tenant_id: UUID
    status: ConnectionStatus
