"""只替换S3运输；校验实际加解密、密文完整性、路径绑定与幂等。"""

from hashlib import sha256
from io import BytesIO
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

from app.core.config import settings
from app.modules.operation_logs import archive
from app.modules.sync_control.archive_storage import ArchiveStorageError
from tests.modules.sync_control.test_archive import ObjectTransport


def storage(monkeypatch, wire, *, key=None):
    captured = []

    def factory(config):
        captured.append(config)
        return wire

    monkeypatch.setattr(archive, "make_s3", factory)
    config = settings.model_copy(
        update={
            "S3_BUCKET": "existing-material",
            "SYNC_ARCHIVE_BUCKET": "",
            "CONNECTION_ENCRYPTION_KEY": key or Fernet.generate_key().decode(),
        }
    )
    result = archive.OperationLogArchive.configured(config)
    assert captured == [config]
    return result, config


def persist(result, tenant_id, run_id):
    payload = b'{"operator":"private-person","bid":1.06}'
    receipt = result.persist(
        tenant_id=tenant_id,
        kind="report",
        run_id=run_id,
        payload=BytesIO(payload),
        sha256_digest=sha256(payload).hexdigest(),
        size=len(payload),
    )
    return receipt


def test_existing_material_bucket_contains_only_encrypted_blob_and_marker(monkeypatch):
    wire = ObjectTransport()
    result, config = storage(monkeypatch, wire)
    tenant_id, run_id = uuid4(), uuid4()
    receipt = persist(result, tenant_id, run_id)
    result.finalize(tenant_id=tenant_id, kind="report", run_id=run_id, receipt=receipt)
    assert result.bucket == config.S3_BUCKET
    assert receipt.key.startswith(archive.PREFIX + f"tenants/{tenant_id}/")
    assert len(wire.objects) == 2
    for sealed in wire.objects.values():
        assert sealed.startswith(archive.MAGIC)
        assert b"private-person" not in sealed
        assert b'"bid"' not in sealed
        assert b'"tenant_id"' not in sealed
    # 跨进程以同一持久主密钥重新创建实例，仍可验证并解析提交凭据。
    reopened, _ = storage(monkeypatch, wire, key=config.CONNECTION_ENCRYPTION_KEY)
    assert reopened.committed(tenant_id=tenant_id, kind="report", run_id=run_id) == (
        [receipt],
        None,
    )
    assert persist(reopened, tenant_id, run_id) == receipt
    assert len(wire.objects) == 2


@pytest.mark.parametrize("failure", ["tamper", "swap_path", "wrong_key", "plaintext"])
def test_corruption_or_cross_object_replay_never_returns_plaintext(
    monkeypatch, failure
):
    wire = ObjectTransport()
    result, _ = storage(monkeypatch, wire)
    tenant_id, run_id = uuid4(), uuid4()
    receipt = persist(result, tenant_id, run_id)
    key = receipt.key
    if failure == "tamper":
        sealed = wire.objects[key]
        wire.objects[key] = sealed[:-1] + bytes([sealed[-1] ^ 1])
    elif failure == "swap_path":
        other = persist(result, uuid4(), uuid4())
        wire.objects[key] = wire.objects[other.key]
    elif failure == "wrong_key":
        result, _ = storage(monkeypatch, wire)
    else:
        wire.objects[key] = b"plaintext operator details must not be accepted" * 4
    with pytest.raises(ArchiveStorageError):
        result._verify(key, expected_sha256=receipt.sha256, expected_size=receipt.size)


def test_missing_encryption_key_fails_before_object_storage(monkeypatch):
    wire = ObjectTransport()
    monkeypatch.setattr(archive, "make_s3", lambda *_: pytest.fail("must not send"))
    config = settings.model_copy(update={"CONNECTION_ENCRYPTION_KEY": ""})
    with pytest.raises(ArchiveStorageError, match="key_invalid"):
        archive.OperationLogArchive.configured(config)
    assert not wire.objects
