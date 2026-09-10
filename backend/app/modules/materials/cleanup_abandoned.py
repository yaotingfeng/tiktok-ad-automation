"""Positive ownership/send checks for abandoned immutable generations."""

from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import ClientError
from sqlmodel import Session, col, select

from .ingest_models import ObjectCleanup, TemporaryMaterialObject
from .models import MaterialAssetOperation
from .storage import storage_error


def abandon_transport(
    session: Session, obj: TemporaryMaterialObject, cleanup: ObjectCleanup
) -> str:
    """Called under the material/object lock. Age is never remote completion."""
    if cleanup.reason not in {"user_cancelled", "abandoned"}:
        raise storage_error("cleanup_unverified")
    if obj.claimed_until and obj.claimed_until > datetime.now(UTC):
        raise storage_error("original_in_use")
    if obj.error_code == "multipart_collecting":
        # Only ListParts was sent. Fence its late callback before allowing Abort.
        obj.claim_token, obj.claimed_until, obj.error_code = None, None, None
    # A lost Create/Complete must recover its identity/result before cleanup.
    if obj.error_code in {
        "multipart_creating",
        "multipart_create_unknown",
        "multipart_completing",
        "multipart_complete_unknown",
    }:
        raise storage_error("object_result_unknown")
    operations = session.exec(
        select(MaterialAssetOperation)
        .where(
            MaterialAssetOperation.tenant_id == obj.tenant_id,
            MaterialAssetOperation.bc_id == obj.bc_id,
            MaterialAssetOperation.material_id == obj.material_id,
            MaterialAssetOperation.path == "upload_original",
            col(MaterialAssetOperation.remote_response)["object_id"].as_string()
            == str(obj.id),
        )
        .with_for_update()
    ).all()
    for operation in operations:
        if (
            operation.remote_response.get("generation") != obj.generation
            or operation.remote_response.get("send_armed")
            or operation.status not in {"failed", "confirmed_absent"}
            or operation.remote_response.get("video_id")
            or (operation.claimed_until and operation.claimed_until > datetime.now(UTC))
        ):
            raise storage_error("original_in_use")
    if obj.received_at is not None:
        return "delete"
    if obj.s3_upload_id:
        return "abort"
    raise storage_error("object_result_unknown")


def no_such_upload(error: ClientError) -> bool:
    return (
        error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404
        and error.response.get("Error", {}).get("Code") == "NoSuchUpload"
    )


def parts_removed(client: Any, obj: TemporaryMaterialObject) -> bool:
    try:
        reply = client.list_parts(
            Bucket=obj.storage_bucket,
            Key=obj.object_key,
            UploadId=obj.s3_upload_id,
            MaxParts=100,
        )
    except ClientError as error:
        if no_such_upload(error):
            return True
        raise
    # Empty is positive only after an exact Abort was sent. Callers persist it.
    return (
        isinstance(reply, dict)
        and reply.get("Parts") == []
        and reply.get("IsTruncated") is False
    )


def abort_original(client: Any, obj: TemporaryMaterialObject) -> None:
    try:
        client.abort_multipart_upload(
            Bucket=obj.storage_bucket,
            Key=obj.object_key,
            UploadId=obj.s3_upload_id,
        )
    except ClientError as error:
        if not no_such_upload(error):
            raise
