from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.integrations.tiktok.contracts.context import ChannelKind

ConnectionStatus = Literal[
    "PENDING_AUTH", "DISCOVERING", "ACTIVE", "REAUTH_REQUIRED", "ERROR", "DISABLED"
]
DiscoveryStatus = Literal[
    "QUEUED", "RUNNING", "ADMISSION_WAIT", "ERROR", "COMPLETE", "CANCELLED"
]


class ConnectionPublic(BaseModel):
    """Explicit allowlist: never serialize ORM credential fields to HTTP."""

    model_config = ConfigDict(from_attributes=True)
    id: UUID
    tenant_id: UUID
    status: ConnectionStatus
    kind: ChannelKind
    display_name: str = ""
    bound_bc_id: str | None = None
    binding_count: int = 0
    pending_binding_count: int = 0
    is_default: bool = False
    authorization_status: str | None = None
    authorization_attempt_id: UUID | None = None
    refresh_status: str | None = None
    read_authorized: bool | None = None
    upload_authorized: bool | None = None
    build_authorized: bool | None = None
    evidence_checked_at: datetime | None = None
    last_discovery: datetime | None = None
    last_authorized_at: datetime | None = None
    discovery_status: DiscoveryStatus | None = None
    error_code: str | None = None


class AccountAccess(BaseModel):
    advertiser_id: str
    bc_id: str
    connection_id: UUID
    currency: str
    timezone: str


class InputLine(BaseModel):
    line_no: int = Field(gt=0)
    raw: str = Field(max_length=1000)


class ResolvedLine(BaseModel):
    line_no: int
    raw: str
    status: Literal[
        "MATCHED", "DUPLICATE", "AMBIGUOUS", "NOT_FOUND", "EMPTY", "BLOCKED"
    ]
    advertiser_id: str | None = None
    candidates: list[str] = Field(default_factory=list)
    duplicate_of: int | None = None
    reason: str | None = None


class ResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bc_id: str = Field(min_length=1, max_length=128)
    lines: list[InputLine] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def unique_line_numbers(self) -> Self:
        if len({line.line_no for line in self.lines}) != len(self.lines):
            raise ValueError("line_no must be unique within one request")
        return self


Availability = Literal[
    "AVAILABLE",
    "PERMISSION_UNKNOWN",
    "NO_ACCESS",
    "OWNERSHIP_CONFLICT",
    "METADATA_INCOMPLETE",
]


class AccountPublic(BaseModel):
    advertiser_id: str
    bc_id: str
    name: str
    currency: str
    timezone: str
    remote_status: str
    ownership_conflict: bool
    can_build: bool
    can_upload: bool
    permission_state: str
    availability: Availability
    checked_at: datetime | None


class BCPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    bc_id: str
    name: str
    ownership_conflict: bool
    is_default: bool = False
    default_connection_id: UUID | None = None
    binding_status: Literal["SYNCING", "ACTIVE", "ERROR", "DISABLED"] | None = None
    last_discovery: datetime | None = None
    discovery_status: DiscoveryStatus | None = None
    error_code: str | None = None


class AuthorizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connection_id: UUID | None = None


class AuthorizationURL(BaseModel):
    url: str


class ConnectionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["DISABLED"]


class DefaultConnectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connection_id: UUID


class ChannelConfiguration(BaseModel):
    kind: ChannelKind
    configured: bool
    status: Literal[
        "READY",
        "NOT_CONFIGURED",
        "INCOMPLETE",
        "CLIENT_UNREGISTERED",
        "PROTOCOL_UNVERIFIED",
        "INVALID",
    ]
    code: str | None = None


class AppConfiguration(BaseModel):
    channels: tuple[ChannelConfiguration, ...]


class McpBindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bc_ids: list[str] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def valid_selection(self) -> Self:
        if len(self.bc_ids) != len(set(self.bc_ids)) or any(
            not value.strip() or value != value.strip() or len(value) > 128
            for value in self.bc_ids
        ):
            raise ValueError("BC selection must contain unique exact identifiers")
        return self


class McpBindingItem(BaseModel):
    bc_id: str
    discovery_run_id: UUID
    status: DiscoveryStatus


class McpBindingResult(BaseModel):
    connection_id: UUID
    items: list[McpBindingItem]


class McpSyncResult(BaseModel):
    discovery_run_id: UUID


class McpCandidateBC(BaseModel):
    bc_id: str
    name: str
    connected: bool = False
    binding_status: Literal["SYNCING", "ACTIVE", "ERROR", "DISABLED"] | None = None


class McpCandidateBCPage(BaseModel):
    items: list[McpCandidateBC]
    page: int
    page_size: int
    total: int


class McpConfiguration(BaseModel):
    configured: bool
    code: str | None = None
    status: Literal["READY", "CLIENT_UNREGISTERED", "PROTOCOL_UNVERIFIED", "INVALID"]
    revocation_supported: bool = False
