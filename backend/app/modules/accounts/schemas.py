from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

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


class AuthorizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connection_id: UUID | None = None


class AuthorizationURL(BaseModel):
    url: str


class ConnectionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["DISABLED"]


class AppConfiguration(BaseModel):
    code: str | None = None
    configured: bool
    status: Literal["READY", "NOT_CONFIGURED", "INCOMPLETE"]
    missing_fields: list[str]
