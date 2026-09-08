from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class TenantContext:
    """Resolved request scope; possession of this value does not grant permission."""

    tenant_id: UUID
    actor_id: UUID
    role: str
