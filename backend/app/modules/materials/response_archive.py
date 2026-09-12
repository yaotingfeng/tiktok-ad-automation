"""Tenant-bound compressed response archives for the existing source upload flow."""

import base64
import gzip
import io
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

from sqlalchemy import Engine
from sqlmodel import Session, select

from app.core.context import TenantContext
from app.core.credentials import decrypt_credentials, encrypt_credentials
from app.core.errors import DomainError
from app.integrations.tiktok.bounded_resources import bounded_session
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.integrations.tiktok.response_capture import (
    VIDEO_RESPONSE_OPERATIONS,
    ProviderResponse,
    ResponseObserver,
)
from app.integrations.tiktok.sdk import SDK_SCOPE_INTERRUPTS
from app.modules.tenants.permissions import require_tenant

from .models import MaterialAssetOperation, MaterialResponseArchive

# 已有传输响应有 8 MiB 限制；工具结果 JSON 序列化可能含转义和双载体。
# 超限明确失败，不把截断正文伪装成完整响应。
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024


def material_response_observer(
    *,
    database_engine: Engine,
    context: TenantContext,
    route: FrozenTikTokRoute,
    material_id: UUID,
    operation_id: UUID,
    advertiser_id: str,
    task_deadline: datetime,
) -> ResponseObserver:
    def save(response: ProviderResponse) -> None:
        if (
            response.advertiser_id != advertiser_id
            or route.tenant_id != context.tenant_id
            or response.operation not in VIDEO_RESPONSE_OPERATIONS
            or response.format
            != (
                "mcp_tool_result_json"
                if route.channel == "OFFICIAL_MCP"
                else "sdk_http_body"
            )
            or not isinstance(response.body, bytes)
            or len(response.body) > MAX_ARCHIVE_BYTES
        ):
            raise DomainError(
                "material_response_archive_failed", "素材响应无法完整留档"
            )
        identity = uuid4()
        digest = sha256(response.body).hexdigest()
        # 完整正文先压缩后加密，重复字段不会撑大主业务行；签名地址留在密文中。
        ciphertext = encrypt_credentials(
            tenant_id=context.tenant_id,
            value={
                "archive_id": str(identity),
                "material_id": str(material_id),
                "operation_id": str(operation_id),
                "body": base64.b64encode(
                    gzip.compress(response.body, mtime=0)
                ).decode(),
                "sha256": digest,
            },
        )
        values = {
            "id": identity,
            "tenant_id": context.tenant_id,
            "bc_id": route.bc_id,
            "material_id": material_id,
            "operation_id": operation_id,
            "advertiser_id": advertiser_id,
            "connection_id": route.connection_id,
            "channel": route.channel,
            "operation": response.operation,
            "format": response.format,
            "http_status": response.http_status,
            "body_bytes": len(response.body),
            "body_sha256": digest,
            "body_ciphertext": ciphertext,
            "received_at": datetime.now(UTC),
        }
        for retry in range(2):
            try:
                with bounded_session(
                    database_engine, task_deadline=task_deadline
                ) as db:
                    with db.begin():
                        op = db.exec(
                            select(MaterialAssetOperation).where(
                                MaterialAssetOperation.id == operation_id,
                                MaterialAssetOperation.tenant_id == context.tenant_id,
                                MaterialAssetOperation.bc_id == route.bc_id,
                                MaterialAssetOperation.material_id == material_id,
                                MaterialAssetOperation.advertiser_id == advertiser_id,
                            )
                        ).one()
                        if op.frozen_route != route.model_dump(mode="json"):
                            raise ValueError("response route mismatch")
                        # 提交回执丢失后的本地重试只复用此归档 ID，不再调用远端。
                        if db.get(MaterialResponseArchive, identity) is None:
                            db.add(MaterialResponseArchive(**values))
                return
            except SDK_SCOPE_INTERRUPTS:
                raise
            except Exception:
                if retry:
                    break
        # 不把原文或数据库异常参数带入队列日志；已有发送按 UNKNOWN 恢复。
        raise DomainError("material_response_archive_failed", "素材响应留档未完成")

    return save


def read_material_response(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    material_id: UUID,
    response_id: UUID,
) -> bytes:
    """供受控运维读取；管理员权限和租户/BC/素材范围均在解密前核对。"""
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="manage"
    )
    row = session.exec(
        select(MaterialResponseArchive).where(
            MaterialResponseArchive.id == response_id,
            MaterialResponseArchive.tenant_id == context.tenant_id,
            MaterialResponseArchive.bc_id == bc_id,
            MaterialResponseArchive.material_id == material_id,
        )
    ).one_or_none()
    if row is None:
        raise DomainError("material_not_found", "未找到当前素材响应")
    value = decrypt_credentials(
        tenant_id=context.tenant_id, ciphertext=row.body_ciphertext
    )
    if (
        value.get("archive_id"),
        value.get("material_id"),
        value.get("operation_id"),
    ) != (str(row.id), str(row.material_id), str(row.operation_id)):
        raise DomainError("material_response_archive_failed", "素材响应归属不一致")
    with gzip.GzipFile(
        fileobj=io.BytesIO(base64.b64decode(value["body"], validate=True))
    ) as compressed:
        body = compressed.read(MAX_ARCHIVE_BYTES + 1)
    if (
        len(body) != row.body_bytes
        or len(body) > MAX_ARCHIVE_BYTES
        or sha256(body).hexdigest() != row.body_sha256
        or row.body_sha256 != value.get("sha256")
    ):
        raise DomainError("material_response_archive_failed", "素材响应摘要不一致")
    return body
