"""Private object storage; never log transport arguments or signed URLs."""

import hashlib
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from uuid import UUID

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]
from botocore.exceptions import (  # type: ignore[import-untyped]
    BotoCoreError,
    ClientError,
)
from sqlmodel import Session, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.modules.materials.models import MaterialFile

MIB = 1024 * 1024
MAX_OBJECT_SIZE = 5 * 1024**4


def storage_error(code: str, *, retryable: bool = False) -> DomainError:
    return DomainError(code, "对象存储操作未完成，请根据状态继续", retryable)


def object_key_for(tenant_id: UUID, material_id: UUID) -> str:
    return f"tenants/{tenant_id}/materials/{material_id}/original"


def part_layout(size: int) -> tuple[int, int]:
    if type(size) is not int or not 0 < size <= MAX_OBJECT_SIZE:
        raise storage_error("invalid_file")
    part_size = max(16 * MIB, ((size + 10000 * MIB - 1) // (10000 * MIB)) * MIB)
    return part_size, (size + part_size - 1) // part_size


def verify_object_size(*, expected: int, actual: int) -> None:
    if expected <= 0 or type(actual) is not int or actual != expected:
        raise storage_error("incomplete_object")


def make_s3() -> Any:
    settings.require_object_storage()
    # Wire DEBUG logs include credentials, signatures and object identifiers.
    for name in (
        "boto3",
        "botocore",
        "botocore.auth",
        "botocore.endpoint",
        "botocore.hooks",
        "botocore.parsers",
        "botocore.regions",
        "botocore.utils",
        "botocore.credentials",
        "urllib3",
        "urllib3.connectionpool",
    ):
        logging.getLogger(name).disabled = True
    return boto3.client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT_URL or None,
        region_name=settings.S3_REGION,
        aws_access_key_id=settings.S3_ACCESS_KEY_ID,
        aws_secret_access_key=settings.S3_SECRET_ACCESS_KEY,
        config=Config(
            signature_version="s3v4",
            connect_timeout=5,
            read_timeout=30,
            retries={"total_max_attempts": 1},
            s3={"addressing_style": "path"},
        ),
    )


def sign_part(
    s3: Any, *, bucket: str, key: str, upload_id: str, part_number: int
) -> str:
    if type(part_number) is not int or not 1 <= part_number <= 10000:
        raise storage_error("invalid_part")
    try:
        return cast(
            str,
            s3.generate_presigned_url(
                "upload_part",
                Params={
                    "Bucket": bucket,
                    "Key": key,
                    "UploadId": upload_id,
                    "PartNumber": part_number,
                },
                ExpiresIn=900,
            ),
        )
    except BotoCoreError, ClientError:
        raise storage_error("object_storage_unavailable", retryable=True) from None


def find_multipart(s3: Any, *, bucket: str, key: str) -> str | None:
    """Recover only an unambiguous server-generated object key; never create again."""
    matches = []
    markers: dict[str, str] = {}
    seen = set()
    while True:
        result = s3.list_multipart_uploads(Bucket=bucket, Prefix=key, **markers)
        for item in result.get("Uploads", []):
            if item.get("Key") == key and isinstance(item.get("UploadId"), str):
                matches.append(item["UploadId"])
        if not result.get("IsTruncated"):
            break
        marker = (result.get("NextKeyMarker"), result.get("NextUploadIdMarker"))
        if (
            not all(isinstance(value, str) and value for value in marker)
            or marker in seen
        ):
            raise storage_error("object_result_unknown")
        seen.add(marker)
        markers = {"KeyMarker": marker[0], "UploadIdMarker": marker[1]}
    return matches[0] if len(matches) == 1 else None


def head_verified(
    s3: Any,
    *,
    bucket: str,
    key: str,
    tenant_id: UUID,
    material_id: UUID,
    expected_size: int,
) -> None:
    result = s3.head_object(Bucket=bucket, Key=key)
    verify_object_size(expected=expected_size, actual=result.get("ContentLength"))
    metadata = result.get("Metadata", {})
    if metadata.get("tenant-id") != str(tenant_id) or metadata.get(
        "material-id"
    ) != str(material_id):
        raise storage_error("object_identity_unverified")


@dataclass(frozen=True)
class OriginalFile:
    path: str
    byte_size: int
    sha256: str
    md5: str


@contextmanager
def open_original(
    *,
    database_engine: Any,
    context: TenantContext,
    bc_id: str,
    material_id: UUID,
    action: str = "upload",
    deadline: datetime | None = None,
    s3: Any = None,
    bucket: str | None = None,
) -> Iterator[OriginalFile]:
    """Read one authorized original into a private, bounded-lifetime local file.

    No session/row lock spans download or the yielded consumer's work. The caller
    reauthorizes its next remote write; these hashes are verified local bytes,
    never an S3 multipart ETag. Cancellation cleans both stream and temp file.
    """
    from app.modules.materials.uploads import require_bc

    if action not in {"read", "upload", "build"}:
        raise storage_error("invalid_file")
    deadline = deadline or datetime.now(UTC) + timedelta(seconds=300)
    if deadline.tzinfo is None:
        raise storage_error("invalid_file")
    with Session(database_engine) as session:
        require_bc(session, context=context, bc_id=bc_id, action=action)
        row = session.exec(
            select(MaterialFile).where(
                MaterialFile.tenant_id == context.tenant_id,
                MaterialFile.bc_id == bc_id,
                MaterialFile.id == material_id,
            )
        ).one_or_none()
        if row is None:
            raise storage_error("material_not_found")
        if row.storage_state != "stored":
            raise storage_error("upload_not_ready")
        key, size, expected_sha, expected_md5 = (
            row.object_key,
            row.byte_size,
            row.sha256,
            row.video_md5,
        )
    client = s3 if s3 is not None else make_s3()
    body = None
    downloaded = False
    try:
        if datetime.now(UTC) >= deadline:
            raise storage_error("object_storage_unavailable", retryable=True)
        result = client.get_object(Bucket=bucket or settings.S3_BUCKET, Key=key)
        body = result.get("Body")
        verify_object_size(expected=size, actual=result.get("ContentLength"))
        metadata = result.get("Metadata", {})
        if metadata.get("tenant-id") != str(context.tenant_id) or metadata.get(
            "material-id"
        ) != str(material_id):
            raise storage_error("object_identity_unverified")
        if body is None:
            raise storage_error("incomplete_object")
        with TemporaryDirectory(prefix="material-original-") as temporary:
            path = Path(temporary) / "original"
            sha, md5, received = hashlib.sha256(), hashlib.md5(), 0
            with path.open("wb") as output:
                path.chmod(0o600)
                while True:
                    if datetime.now(UTC) >= deadline:
                        raise storage_error(
                            "object_storage_unavailable", retryable=True
                        )
                    chunk = body.read(8 * MIB)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > size:
                        raise storage_error("incomplete_object")
                    sha.update(chunk)
                    md5.update(chunk)
                    output.write(chunk)
            verify_object_size(expected=size, actual=received)
            if (expected_sha and sha.hexdigest() != expected_sha) or (
                expected_md5 and md5.hexdigest() != expected_md5
            ):
                raise storage_error("incomplete_object")
            body.close()
            body = None
            downloaded = True
            yield OriginalFile(
                path=str(path),
                byte_size=size,
                sha256=sha.hexdigest(),
                md5=md5.hexdigest(),
            )
    except BotoCoreError, ClientError, OSError:
        if downloaded:
            raise
        raise storage_error("object_storage_unavailable", retryable=True) from None
    finally:
        if body is not None:
            body.close()
        if s3 is None:
            client.close()
