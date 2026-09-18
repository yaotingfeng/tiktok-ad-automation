from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.core.usernames import Username
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


class MemberUserCreate(BaseModel):
    """租户管理员只能创建当前租户的普通登录账号。"""

    model_config = ConfigDict(extra="forbid")
    username: Username
    full_name: str | None = Field(default=None, max_length=255)
    password: str = Field(min_length=8, max_length=128)
    role: MemberRole


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
