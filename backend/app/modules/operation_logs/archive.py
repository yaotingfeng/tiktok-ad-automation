"""日志原文与提交凭据先认证加密，再复用素材桶；桶内永不保存明文。"""

import base64
import io
import os
from typing import Any
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.core.config import Settings, settings
from app.core.errors import ConfigurationError
from app.modules.materials.storage import make_s3
from app.modules.sync_control.archive_storage import (
    MAX_ARCHIVE_BYTES,
    ArchiveStorageError,
    S3RunArchive,
    archive_key,
    archive_prefix,
)

PREFIX = "operation-logs/encrypted-v1/"
MAGIC = b"TKADA-OPLOG-AESGCM-V1\n"
OVERHEAD = len(MAGIC) + 12 + 16


class _EncryptedTransport:
    def __init__(self, client: Any, cipher: AESGCM):
        self.client, self.cipher = client, cipher

    @staticmethod
    def _context(bucket: str, key: str) -> bytes:
        # 密文绑定实际桶与完整租户路径；不能交换不同租户/导出的对象蒙混回读。
        if not key.startswith(PREFIX):
            raise ArchiveStorageError("operation_log_archive_path_invalid")
        return (bucket + "\0" + key).encode()

    def put_object(self, **kwargs: Any) -> Any:
        body = kwargs["Body"]
        payload = body.read(MAX_ARCHIVE_BYTES + 1) if hasattr(body, "read") else body
        if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_ARCHIVE_BYTES:
            raise ArchiveStorageError("archive_too_large")
        if len(payload) != kwargs["ContentLength"]:
            raise ArchiveStorageError("archive_length_mismatch")
        nonce = os.urandom(12)
        sealed = (
            MAGIC
            + nonce
            + self.cipher.encrypt(
                nonce, payload, self._context(kwargs["Bucket"], kwargs["Key"])
            )
        )
        # 原始日志、marker正文和明文摘要metadata均不能写入可能公开的素材桶。
        return self.client.put_object(
            **{
                **kwargs,
                "Body": sealed,
                "ContentLength": len(sealed),
                "ContentType": "application/octet-stream",
                "Metadata": {"encryption": "tkada-oplog-aesgcm-v1"},
            }
        )

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        response = self.client.get_object(**kwargs)
        body = response["Body"]
        try:
            size = response.get("ContentLength")
            if (
                type(size) is not int
                or not OVERHEAD < size <= MAX_ARCHIVE_BYTES + OVERHEAD
            ):
                raise ArchiveStorageError("archive_length_mismatch")
            sealed = body.read(size + 1)
            if len(sealed) != size or not sealed.startswith(MAGIC):
                raise ArchiveStorageError("operation_log_archive_ciphertext_invalid")
            offset = len(MAGIC)
            try:
                payload = self.cipher.decrypt(
                    sealed[offset : offset + 12],
                    sealed[offset + 12 :],
                    self._context(kwargs["Bucket"], kwargs["Key"]),
                )
            except InvalidTag:
                raise ArchiveStorageError(
                    "operation_log_archive_authentication_failed"
                ) from None
            return {"Body": io.BytesIO(payload), "ContentLength": len(payload)}
        finally:
            body.close()

    def list_objects_v2(self, **kwargs: Any) -> Any:
        self._context(kwargs["Bucket"], kwargs["Prefix"])
        return self.client.list_objects_v2(**kwargs)


class OperationLogArchive(S3RunArchive):
    @classmethod
    def configured(cls, config: Settings | None = None) -> OperationLogArchive:
        config = config or settings
        try:
            config.require_connection_encryption()
            master = base64.urlsafe_b64decode(config.CONNECTION_ENCRYPTION_KEY)
        except ConfigurationError, ValueError, TypeError:
            raise ArchiveStorageError("operation_log_archive_key_invalid") from None
        # 使用既有持久加密主密钥派生独立用途密钥，不把凭据加密密钥直接用于文件。
        derived = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"tkada-operation-log-archive-v1",
            info=b"aes-256-gcm-object-storage",
        ).derive(master)
        return cls(
            _EncryptedTransport(make_s3(config), AESGCM(derived)), config.S3_BUCKET
        )

    def prefix(self, tenant_id: UUID, kind: str, run_id: UUID) -> str:
        return PREFIX + archive_prefix(tenant_id, kind, run_id)

    def key(self, tenant_id: UUID, kind: str, run_id: UUID, digest: str) -> str:
        return PREFIX + archive_key(tenant_id, kind, run_id, digest)
