from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.modules.tenants.permissions import Role

MemberRole = Literal["tenant_admin", "operator", "viewer"]


class TenantSummary(BaseModel):
    id: UUID
    name: str
    active: bool
    role: Role


class TenantCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    administrator_id: UUID


class TenantUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=120)
    active: bool | None = None


class MemberSet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: UUID
    role: MemberRole
    active: bool


class MemberPublic(BaseModel):
    tenant_id: UUID
    user_id: UUID
    role: MemberRole
    active: bool
    username: str
    full_name: str | None
    user_active: bool


class UserCandidate(BaseModel):
    id: UUID
    username: str
    full_name: str | None
