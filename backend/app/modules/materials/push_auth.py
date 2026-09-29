"""系统接入验签；租户范围和执行身份由受控服务端配置提供。"""

import hashlib
import hmac
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from pydantic import BaseModel, Field
from sqlmodel import Session

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.modules.tenants.models import TenantMembership
from app.modules.tenants.permissions import require_tenant


class PushClient(BaseModel):
    model_config = {"extra": "forbid"}
    secret: str = Field(min_length=32, repr=False)
    actor_id: UUID
    tenant_ids: set[UUID] = Field(min_length=1)
    allowed_hosts: set[str] = Field(min_length=1)


@dataclass(frozen=True)
class PushIdentity:
    key_id: str
    request_id: UUID
    client: PushClient


def get_client(key_id: str) -> PushClient:
    try:
        return PushClient.model_validate(settings.MATERIAL_PUSH_CLIENTS[key_id])
    except KeyError, ValueError, TypeError:
        raise DomainError("push_unauthorized", "推送凭证无效或已停用") from None


def signature(
    secret: str, method: str, path: str, timestamp: str, request_id: str, body: bytes
) -> str:
    message = "\n".join(
        (method, path, timestamp, request_id, hashlib.sha256(body).hexdigest())
    )
    return (
        "sha256="
        + hmac.new(secret.encode(), message.encode("utf-8"), hashlib.sha256).hexdigest()
    )


def authenticate(
    *, method: str, path: str, headers: Mapping[str, str], body: bytes
) -> PushIdentity:
    try:
        key_id = headers.get("x-key-id", "")
        timestamp = headers.get("x-timestamp", "")
        request_id = headers.get("x-request-id", "")
        received = headers.get("x-signature", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", key_id) or not re.fullmatch(
            r"[0-9]{10}", timestamp
        ):
            raise ValueError
        identity = UUID(request_id)
        if str(identity) != request_id or abs(time.time() - int(timestamp)) > 300:
            raise ValueError
        client = get_client(key_id)
        expected = signature(client.secret, method, path, timestamp, request_id, body)
        if not re.fullmatch(
            r"sha256=[0-9a-f]{64}", received
        ) or not hmac.compare_digest(expected, received):
            raise ValueError
    except ValueError, TypeError, AttributeError:
        raise DomainError("push_unauthorized", "签名无效或请求已过期") from None
    return PushIdentity(key_id, identity, client)


def require_push_tenant(
    session: Session, client: PushClient, tenant_id: UUID
) -> TenantContext:
    if tenant_id not in client.tenant_ids:
        raise DomainError("push_forbidden", "接入方没有该租户的推送权限")
    context = require_tenant(
        session, actor_id=client.actor_id, tenant_id=tenant_id, action="upload"
    )
    # 入库与原件用途外键要求实际成员，平台管理员身份也不能隐式代替成员绑定。
    member = session.get(
        TenantMembership, (tenant_id, client.actor_id), populate_existing=True
    )
    if (
        not member
        or not member.active
        or member.role not in {"tenant_admin", "operator"}
    ):
        raise DomainError("push_forbidden", "推送执行用户必须是租户的有效上传成员")
    return context
