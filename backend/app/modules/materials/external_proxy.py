"""将外部素材以 TikTok 可读取的响应头短时中转。"""

import hashlib
import hmac
from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID

import urllib3
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from sqlmodel import Session, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.db import engine
from app.modules.materials.ingest_models import IngestSession, IngestSessionFile
from app.modules.materials.models import MaterialAssetOperation, MaterialFile
from app.modules.materials.push_worker import external_source_url

from .push_transport import public_address, validate_url

PROXY_TTL_SECONDS = 900
_SIGNATURE_BYTES = 32


def _signature(*, tenant_id: UUID, material_id: UUID, operation_id: UUID, expires: int) -> str:
    payload = f"{tenant_id}:{material_id}:{operation_id}:{expires}".encode()
    return hmac.new(settings.SECRET_KEY.encode(), payload, hashlib.sha256).hexdigest()


def external_proxy_url(
    *, tenant_id: UUID, material_id: UUID, operation_id: UUID, now: datetime | None = None
) -> str:
    current = now or datetime.now(UTC)
    expires = int(current.timestamp()) + PROXY_TTL_SECONDS
    signature = _signature(
        tenant_id=tenant_id,
        material_id=material_id,
        operation_id=operation_id,
        expires=expires,
    )
    base = settings.FRONTEND_HOST.rstrip("/") + settings.API_V1_STR
    return (
        f"{base}/integrations/materials/source/{material_id}/{operation_id}"
        f"?tenant_id={tenant_id}&expires={expires}&signature={signature}"
    )


def _verify_signature(
    *, tenant_id: UUID, material_id: UUID, operation_id: UUID, expires: int, signature: str
) -> None:
    if expires < int(datetime.now(UTC).timestamp()) or len(signature) != _SIGNATURE_BYTES * 2:
        raise HTTPException(status_code=404, detail="source unavailable")
    expected = _signature(
        tenant_id=tenant_id,
        material_id=material_id,
        operation_id=operation_id,
        expires=expires,
    )
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=404, detail="source unavailable")


def stream_external_source(
    *,
    tenant_id: UUID,
    material_id: UUID,
    operation_id: UUID,
    expires: int,
    signature: str,
) -> StreamingResponse:
    """校验冻结中的一次发送后，再把外部 GET 流转发给 TikTok。"""
    _verify_signature(
        tenant_id=tenant_id,
        material_id=material_id,
        operation_id=operation_id,
        expires=expires,
        signature=signature,
    )
    with Session(engine) as db:
        operation = db.get(MaterialAssetOperation, operation_id)
        material = db.get(MaterialFile, material_id)
        if (
            operation is None
            or material is None
            or operation.tenant_id != tenant_id
            or operation.material_id != material_id
            or operation.status != "sending"
            or operation.attempt_token is None
        ):
            raise HTTPException(status_code=404, detail="source unavailable")
        # 外部来源通过其不可变推送记录校验执行用户；代理不接受浏览器身份。
        row = db.exec(
            select(IngestSessionFile).where(IngestSessionFile.material_id == material_id)
        ).first()
        parent = db.get(IngestSession, row.session_id) if row else None
        if parent is None:
            raise HTTPException(status_code=404, detail="source unavailable")
        url = external_source_url(
            db,
            context=TenantContext(tenant_id, parent.actor_id, "operator"),
            material_id=material_id,
        )
        parsed = validate_url(url)
        assert parsed.hostname
        address = public_address(parsed.hostname)
        target = parsed.path + (("?" + parsed.query) if parsed.query else "")
        expected_bytes, mime_type = material.byte_size, material.mime_type

    pool = urllib3.HTTPSConnectionPool(
        address,
        port=443,
        server_hostname=parsed.hostname,
        assert_hostname=parsed.hostname,
        cert_reqs="CERT_REQUIRED",
        maxsize=1,
        block=True,
    )
    response = pool.urlopen(
        "GET",
        target,
        headers={"Host": parsed.hostname, "Accept-Encoding": "identity"},
        preload_content=False,
        redirect=False,
        retries=False,
        timeout=urllib3.Timeout(connect=5, read=30, total=120),
    )
    if response.status != 200 or response.headers.get("Content-Encoding", "identity").lower() != "identity":
        response.close()
        response.release_conn()
        pool.close()
        raise HTTPException(status_code=502, detail="source unavailable")

    def body() -> Iterator[bytes]:
        received = 0
        try:
            while True:
                chunk = response.read(min(1024 * 1024, expected_bytes - received + 1))
                if not chunk:
                    break
                received += len(chunk)
                if received > expected_bytes:
                    raise RuntimeError("source too large")
                yield chunk
            if received != expected_bytes:
                raise RuntimeError("source incomplete")
        finally:
            response.close()
            response.release_conn()
            pool.close()

    return StreamingResponse(
        body(),
        media_type=mime_type,
        headers={
            "Content-Length": str(expected_bytes),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
