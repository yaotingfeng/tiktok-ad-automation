"""R2 contracts at the real storage configuration and request boundary."""

from urllib.parse import parse_qs, urlsplit

import pytest

from app.core.config import Settings
from app.core.errors import ConfigurationError, DomainError
from app.modules.materials import storage, uploads
from tests.modules.materials.test_object_uploads import (
    FakeS3,
    start,
)
from tests.modules.materials.test_object_uploads import (
    upload_owner as upload_owner,
)


def r2_settings(**overrides):
    values = {
        "PROJECT_NAME": "R2 contract",
        "SECRET_KEY": "r2-contract-signing-key-at-least-32-bytes",
        "DATABASE_URL": "postgresql://unused:unused@127.0.0.1/unused_test",
        "FIRST_SUPERUSER": "admin",
        "FIRST_SUPERUSER_PASSWORD": "test-password-only",
        "OBJECT_STORAGE_PROVIDER": "r2",
        "S3_ENDPOINT_URL": "https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com",
        "S3_BUCKET": "private-videos",
        "S3_REGION": "us-east-1",
        "S3_ACCESS_KEY_ID": "test-access-key-only",
        "S3_SECRET_ACCESS_KEY": "test-secret-key-only",
        "FRONTEND_HOST": "https://app.example.test",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_r2_factory_signs_for_auto_region_without_public_endpoint(monkeypatch):
    monkeypatch.setattr(storage, "settings", r2_settings())
    client = storage.make_s3()
    try:
        url = client.generate_presigned_url(
            "get_object",
            Params={"Bucket": "private-videos", "Key": "test-object"},
            ExpiresIn=120,
        )
        parsed = urlsplit(url)
        assert (
            parsed.hostname
            == "0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com"
        )
        assert "/auto/s3/aws4_request" in parse_qs(parsed.query)["X-Amz-Credential"][0]
        assert parse_qs(parsed.query)["X-Amz-Expires"] == ["120"]
    finally:
        client.close()


@pytest.mark.parametrize(
    "endpoint",
    [
        "",
        "http://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com",
        "https://public-example.r2.dev",
        "https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com.evil.test",
        "https://secret-marker@0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com",
        "https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com/bucket",
        "https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com?key=secret-marker",
    ],
)
def test_r2_configuration_rejects_unsigned_api_namespace_errors_without_secrets(
    endpoint,
):
    configured = r2_settings(S3_ENDPOINT_URL=endpoint)
    with pytest.raises(ConfigurationError) as raised:
        configured.require_object_storage()
    assert raised.value.code == "object_storage_unconfigured"
    assert "S3_ENDPOINT_URL" in raised.value.fields
    assert "secret-marker" not in str(raised.value)


def test_r2_multipart_request_omits_unsupported_acl(monkeypatch, upload_owner):
    configured = r2_settings()
    monkeypatch.setattr(storage, "settings", configured)
    monkeypatch.setattr(uploads, "settings", configured)
    remote = FakeS3()
    result = start(upload_owner, remote)
    requests = [args for name, args in remote.calls if name == "create"]
    assert len(requests) == 1
    request = requests[0]
    assert "ACL" not in request
    assert request["Metadata"]["tenant-id"] == str(upload_owner.tenant_id)
    assert request["Metadata"]["material-id"] == str(result.files[0].material_id)
    assert request["ContentType"] == "video/mp4"


def test_legacy_s3_still_accepts_its_explicit_private_endpoint(monkeypatch):
    configured = r2_settings(
        OBJECT_STORAGE_PROVIDER="s3",
        S3_ENDPOINT_URL="http://127.0.0.1:19000",
        S3_REGION="us-east-1",
    )
    configured.require_object_storage()
    monkeypatch.setattr(storage, "settings", configured)
    client = storage.make_s3()
    try:
        assert client.meta.region_name == "us-east-1"
    finally:
        client.close()


def test_part_signature_binds_bytes_to_reserved_layout(monkeypatch):
    monkeypatch.setattr(storage, "settings", r2_settings())
    client = storage.make_s3()
    try:
        signed = storage.sign_part(
            client,
            bucket="private-videos",
            key="test-object",
            upload_id="test-upload",
            part_number=1,
            byte_size=100,
        )
        assert parse_qs(urlsplit(signed).query)["X-Amz-SignedHeaders"] == [
            "content-length;host"
        ]
    finally:
        client.close()


def test_object_factory_keeps_pinned_namespace_after_deployment_bucket_changes(
    monkeypatch,
):
    from types import SimpleNamespace

    config = r2_settings()
    monkeypatch.setattr(storage, "settings", config)
    calls = []
    monkeypatch.setattr(
        storage.boto3,
        "client",
        lambda *args, **kwargs: calls.append(kwargs) or object(),
    )
    obj = SimpleNamespace(
        storage_provider="r2",
        storage_bucket="old-private-bucket",
        storage_endpoint="https://previous.r2.cloudflarestorage.com",
    )
    storage.make_object_s3(obj)
    assert calls[0]["endpoint_url"] == obj.storage_endpoint
    assert calls[0]["region_name"] == "auto"
    obj.storage_provider = None
    with pytest.raises(DomainError) as error:
        storage.make_object_s3(obj)
    assert error.value.code == "object_namespace_unverified"


def test_part_signing_uses_the_same_bounded_lifetime_as_its_permission(monkeypatch):
    monkeypatch.setattr(storage, "settings", r2_settings())
    client = storage.make_s3()
    try:
        url = storage.sign_part(
            client,
            bucket="private",
            key="owned",
            upload_id="test-upload",
            part_number=1,
            byte_size=100,
            expires_in=60,
        )
        assert parse_qs(urlsplit(url).query)["X-Amz-Expires"] == ["60"]
        with pytest.raises(DomainError):
            storage.sign_part(
                client,
                bucket="private",
                key="owned",
                upload_id="test-upload",
                part_number=1,
                expires_in=901,
            )
    finally:
        client.close()
