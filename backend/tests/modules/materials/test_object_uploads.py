from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError
from sqlalchemy import delete, inspect
from sqlmodel import Session, SQLModel, select

from app.core.db import engine
from app.core.errors import DomainError
from app.jobs.models import PendingDispatch
from app.models import User
from app.modules.accounts.models import TenantBC
from app.modules.materials.models import MaterialFile, ObjectUpload, UploadBatch
from app.modules.tenants.models import Tenant, TenantMembership
from tests.modules.conftest import create_context


class FakeS3:
    def __init__(self):
        self.calls = []
        self.uploads = {}
        self.objects = {}
        self.callback = None
        self.complete_mode = "ok"
        self.create_mode = "ok"

    def close(self):
        pass

    def _call(self, name, values):
        self.calls.append((name, values))
        if self.callback:
            self.callback(name, values)

    def create_multipart_upload(self, **values):
        self._call("create", values)
        identity = str(uuid4())
        self.uploads[identity] = values
        if self.create_mode == "unknown":
            raise ReadTimeoutError(endpoint_url="https://storage.invalid")
        return {"UploadId": identity}

    def list_multipart_uploads(self, **values):
        self._call("list", values)
        return {
            "Uploads": [
                {"Key": row["Key"], "UploadId": key}
                for key, row in self.uploads.items()
                if row["Key"].startswith(values["Prefix"])
            ],
            "IsTruncated": False,
        }

    def generate_presigned_url(self, operation, **values):
        self._call("sign", {"operation": operation, **values})
        return "https://storage.invalid/opaque-signed-upload"

    def complete_multipart_upload(self, **values):
        self._call("complete", values)
        if self.complete_mode == "invalid":
            raise ClientError(
                {"Error": {"Code": "InvalidPart"}}, "CompleteMultipartUpload"
            )
        upload = self.uploads[values["UploadId"]]
        if self.complete_mode != "absent":
            self.objects[values["Key"]] = {
                "ContentLength": int(upload["Metadata"]["byte-size"]),
                "Metadata": upload["Metadata"],
            }
        if self.complete_mode in {"unknown", "absent"}:
            raise ReadTimeoutError(endpoint_url="https://storage.invalid")
        return {"ETag": "multipart-is-not-md5"}

    def head_object(self, **values):
        self._call("head", values)
        if values["Key"] not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return self.objects[values["Key"]]


@pytest.fixture
def upload_owner(monkeypatch):
    from app.jobs.tasks import _DISPATCH_TASKS

    monkeypatch.setitem(_DISPATCH_TASKS, "materials.upload_original", "resources")
    with Session(engine) as session, session.begin():
        context = create_context(session)
        session.add(TenantBC(tenant_id=context.tenant_id, bc_id="bc-a"))
        from app.modules.accounts.connection_models import (
            BCConnectionBinding,
            BCDefaultRoute,
        )
        from app.modules.accounts.models import TikTokConnection

        connection = TikTokConnection(tenant_id=context.tenant_id, status="ACTIVE")
        session.add(connection)
        session.flush()
        session.add(
            BCConnectionBinding(
                tenant_id=context.tenant_id,
                bc_id="bc-a",
                connection_id=connection.id,
                kind="OFFICIAL_API",
            )
        )
        session.flush()
        session.add(
            BCDefaultRoute(
                tenant_id=context.tenant_id, bc_id="bc-a", connection_id=connection.id
            )
        )
    try:
        yield context
    finally:
        with Session(engine) as session, session.begin():
            for table in reversed(SQLModel.metadata.sorted_tables):
                if "tenant_id" in table.c:
                    session.execute(
                        delete(table).where(table.c.tenant_id == context.tenant_id)
                    )
            session.execute(delete(Tenant).where(Tenant.id == context.tenant_id))
            session.execute(delete(User).where(User.id == context.actor_id))


def start(context, s3, *, request_id=None, files=None):
    from app.modules.materials.schemas import UploadFileRequest
    from app.modules.materials.uploads import (
        initialize_object_upload,
        start_upload_batch,
    )

    with Session(engine) as session, session.begin():
        batch = start_upload_batch(
            session,
            context=context,
            bc_id="bc-a",
            request_id=request_id or uuid4(),
            files=files
            or [
                UploadFileRequest(
                    file_name="../Moon.mp4", size=100, mime_type="video/mp4"
                )
            ],
        )
    for file in batch.files:
        initialize_object_upload(
            database_engine=engine,
            context=context,
            material_id=file.material_id,
            s3=s3,
            bucket="private",
        )
    return batch


def part():
    from app.modules.materials.schemas import UploadedPart

    return [UploadedPart(part_number=1, etag='"fake-etag"')]


def test_object_upload_has_durable_network_claim():
    token, expiry = uuid4(), datetime.now(UTC)
    row = ObjectUpload(attempt_token=token, claimed_until=expiry)
    assert row.attempt_token == token and row.claimed_until == expiry
    columns = {
        column["name"] for column in inspect(engine).get_columns("object_upload")
    }
    assert {"attempt_token", "claimed_until"} <= columns


def test_storage_identity_size_and_large_part_count():
    from app.modules.materials.storage import (
        object_key_for,
        part_layout,
        verify_object_size,
    )

    tenant, material = uuid4(), uuid4()
    assert (
        object_key_for(tenant, material)
        == f"tenants/{tenant}/materials/{material}/original"
    )
    assert part_layout(16 * 1024 * 1024 + 1) == (16 * 1024 * 1024, 2)
    size, count = part_layout(5 * 1024**4)
    assert count <= 10000 and size >= 16 * 1024**2
    with pytest.raises(DomainError) as error:
        verify_object_size(expected=100, actual=90)
    assert error.value.code == "incomplete_object"


def test_start_is_idempotent_and_binds_private_object(upload_owner):
    from app.modules.materials.schemas import UploadFileRequest
    from app.modules.materials.uploads import start_upload_batch

    s3, request = FakeS3(), uuid4()
    batch = start(upload_owner, s3, request_id=request)
    same = start(upload_owner, s3, request_id=request)
    assert same.batch_id == batch.batch_id
    assert len([call for call in s3.calls if call[0] == "create"]) == 1
    data = s3.calls[0][1]
    assert data["ACL"] == "private" and "../Moon" not in data["Key"]
    assert data["Metadata"]["material-id"] == str(batch.files[0].material_id)
    assert not {"object_key", "s3_upload_id"} & batch.files[0].model_dump().keys()
    with Session(engine) as session, pytest.raises(DomainError) as error:
        start_upload_batch(
            session,
            context=upload_owner,
            bc_id="bc-a",
            request_id=request,
            files=[
                UploadFileRequest(
                    file_name="different.mp4", size=100, mime_type="video/mp4"
                )
            ],
        )
    assert error.value.code == "idempotency_conflict"


def test_sign_reloads_authority_and_uses_exact_part_scope(upload_owner):
    from app.modules.materials.uploads import sign_upload_part

    s3 = FakeS3()
    material_id = start(upload_owner, s3).files[0].material_id
    signed = sign_upload_part(
        database_engine=engine,
        context=upload_owner,
        material_id=material_id,
        part_number=1,
        s3=s3,
        bucket="private",
    )
    assert signed.expires_in == 900
    call = s3.calls[-1][1]
    assert call["ExpiresIn"] == 900 and call["Params"]["PartNumber"] == 1
    with pytest.raises(DomainError) as error:
        sign_upload_part(
            database_engine=engine,
            context=upload_owner,
            material_id=material_id,
            part_number=2,
            s3=s3,
            bucket="private",
        )
    assert error.value.code == "invalid_part"
    with Session(engine) as session, session.begin():
        session.get(
            TenantMembership, (upload_owner.tenant_id, upload_owner.actor_id)
        ).role = "viewer"
    count = len(s3.calls)
    with pytest.raises(DomainError):
        sign_upload_part(
            database_engine=engine,
            context=upload_owner,
            material_id=material_id,
            part_number=1,
            s3=s3,
            bucket="private",
        )
    assert len(s3.calls) == count


def test_completion_commits_stored_and_one_outbox_without_holding_lock(upload_owner):
    from app.modules.materials.uploads import complete_object_upload

    s3 = FakeS3()
    material_id = start(upload_owner, s3).files[0].material_id

    def inspect_lock(name, _values):
        if name not in {"complete", "head"}:
            return
        with Session(engine) as session, session.begin():
            session.connection().exec_driver_sql("SET LOCAL lock_timeout = '250ms'")
            row = session.exec(
                select(ObjectUpload)
                .where(ObjectUpload.material_id == material_id)
                .with_for_update()
            ).one()
            assert row.attempt_token is not None

    s3.callback = inspect_lock
    task = complete_object_upload(
        database_engine=engine,
        context=upload_owner,
        material_id=material_id,
        parts=part(),
        s3=s3,
        bucket="private",
    )
    assert task == complete_object_upload(
        database_engine=engine,
        context=upload_owner,
        material_id=material_id,
        parts=part(),
        s3=s3,
        bucket="private",
    )
    with Session(engine) as session:
        file = session.get(MaterialFile, material_id)
        assert file.storage_state == "stored" and file.video_md5 is None
        dispatches = session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == upload_owner.tenant_id
            )
        ).all()
        assert len(dispatches) == 1 and dispatches[0].payload == {
            "material_id": str(material_id)
        }
        assert (
            session.get(UploadBatch, start_batch_id(session, material_id)).status
            == "stored"
        )
    assert len([call for call in s3.calls if call[0] == "complete"]) == 1


def start_batch_id(session, material_id):
    return (
        session.exec(
            select(ObjectUpload).where(ObjectUpload.material_id == material_id)
        )
        .one()
        .batch_id
    )


def test_unknown_completion_recovers_by_head_without_replay(upload_owner):
    from app.modules.materials.uploads import complete_object_upload

    s3 = FakeS3()
    material_id = start(upload_owner, s3).files[0].material_id
    s3.complete_mode = "unknown"
    task = complete_object_upload(
        database_engine=engine,
        context=upload_owner,
        material_id=material_id,
        parts=part(),
        s3=s3,
        bucket="private",
    )
    assert task
    assert [name for name, _ in s3.calls].count("complete") == 1


def test_unknown_absent_object_never_replays_or_claims_retry(upload_owner):
    from app.modules.materials.uploads import complete_object_upload, get_upload_batch

    s3 = FakeS3()
    batch = start(upload_owner, s3)
    material_id = batch.files[0].material_id
    s3.complete_mode = "absent"
    for _ in range(2):
        with pytest.raises(DomainError) as error:
            complete_object_upload(
                database_engine=engine,
                context=upload_owner,
                material_id=material_id,
                parts=part(),
                s3=s3,
                bucket="private",
            )
        assert error.value.code == "object_result_unknown" and not error.value.retryable
    assert [name for name, _ in s3.calls].count("complete") == 1
    with Session(engine) as session:
        result = get_upload_batch(
            session, context=upload_owner, batch_id=batch.batch_id
        )
        assert (
            result.files[0].status == "result_unknown" and not result.files[0].can_retry
        )
        assert not session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == upload_owner.tenant_id
            )
        ).all()


def test_bad_object_size_never_marks_stored_or_enqueues(upload_owner):
    from app.modules.materials.uploads import complete_object_upload

    s3 = FakeS3()
    material_id = start(upload_owner, s3).files[0].material_id

    def bad_size(name, values):
        if name == "head":
            s3.objects[values["Key"]]["ContentLength"] = 90

    s3.callback = bad_size
    with pytest.raises(DomainError) as error:
        complete_object_upload(
            database_engine=engine,
            context=upload_owner,
            material_id=material_id,
            parts=part(),
            s3=s3,
            bucket="private",
        )
    assert error.value.code == "incomplete_object"
    with Session(engine) as session:
        assert session.get(MaterialFile, material_id).storage_state == "receiving"
        assert not session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == upload_owner.tenant_id
            )
        ).all()


def test_expired_create_claim_recovers_unique_upload_without_recreating(upload_owner):
    from app.modules.materials.schemas import UploadFileRequest
    from app.modules.materials.uploads import (
        initialize_object_upload,
        start_upload_batch,
    )

    s3 = FakeS3()
    s3.create_mode = "unknown"
    with Session(engine) as session, session.begin():
        batch = start_upload_batch(
            session,
            context=upload_owner,
            bc_id="bc-a",
            request_id=uuid4(),
            files=[
                UploadFileRequest(file_name="Moon.mp4", size=100, mime_type="video/mp4")
            ],
        )
    material_id = batch.files[0].material_id
    initialize_object_upload(
        database_engine=engine,
        context=upload_owner,
        material_id=material_id,
        s3=s3,
        bucket="private",
    )
    with Session(engine) as session:
        row = session.exec(
            select(ObjectUpload).where(ObjectUpload.material_id == material_id)
        ).one()
        assert row.status == "receiving" and row.s3_upload_id
    assert [name for name, _ in s3.calls].count("create") == 1


def test_finalizer_rollback_recovers_object_without_second_complete(
    upload_owner, monkeypatch
):
    from app.modules.materials import uploads

    s3 = FakeS3()
    material_id = start(upload_owner, s3).files[0].material_id
    original = uploads.finish_upload

    def rollback(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(uploads, "finish_upload", rollback)
    with pytest.raises(RuntimeError):
        uploads.complete_object_upload(
            database_engine=engine,
            context=upload_owner,
            material_id=material_id,
            parts=part(),
            s3=s3,
            bucket="private",
        )
    with Session(engine) as session, session.begin():
        assert session.get(MaterialFile, material_id).storage_state == "receiving"
        assert not session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == upload_owner.tenant_id
            )
        ).all()
        row = session.exec(
            select(ObjectUpload).where(ObjectUpload.material_id == material_id)
        ).one()
        row.claimed_until = datetime.now(UTC) - timedelta(seconds=1)
    monkeypatch.setattr(uploads, "finish_upload", original)
    assert uploads.complete_object_upload(
        database_engine=engine,
        context=upload_owner,
        material_id=material_id,
        parts=part(),
        s3=s3,
        bucket="private",
    )
    assert [name for name, _ in s3.calls].count("complete") == 1


def test_concurrent_finish_has_one_sender_and_one_durable_task(upload_owner):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from app.modules.materials.uploads import complete_object_upload

    s3 = FakeS3()
    material_id = start(upload_owner, s3).files[0].material_id
    entered, release = Event(), Event()

    def hold(name, _values):
        if name == "complete":
            entered.set()
            assert release.wait(5)

    s3.callback = hold
    args = {
        "database_engine": engine,
        "context": upload_owner,
        "material_id": material_id,
        "parts": part(),
        "s3": s3,
        "bucket": "private",
    }
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(complete_object_upload, **args)
        assert entered.wait(5)
        try:
            with pytest.raises(DomainError) as error:
                complete_object_upload(**args)
            assert error.value.code == "upload_in_progress"
        finally:
            release.set()
        task = first.result(5)
    assert task and [name for name, _ in s3.calls].count("complete") == 1


def test_revocation_during_network_does_not_promote_or_enqueue(upload_owner):
    from app.modules.materials.uploads import complete_object_upload

    s3 = FakeS3()
    material_id = start(upload_owner, s3).files[0].material_id

    def revoke(name, _values):
        if name == "head":
            with Session(engine) as session, session.begin():
                session.get(
                    TenantMembership, (upload_owner.tenant_id, upload_owner.actor_id)
                ).role = "viewer"

    s3.callback = revoke
    with pytest.raises(DomainError) as error:
        complete_object_upload(
            database_engine=engine,
            context=upload_owner,
            material_id=material_id,
            parts=part(),
            s3=s3,
            bucket="private",
        )
    assert error.value.code == "action_forbidden"
    with Session(engine) as session:
        assert session.get(MaterialFile, material_id).storage_state == "receiving"
        assert not session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == upload_owner.tenant_id
            )
        ).all()


def test_open_original_is_scoped_streaming_and_cleans_private_file(upload_owner):
    import hashlib
    import io
    from pathlib import Path

    from app.modules.materials.storage import open_original
    from app.modules.materials.uploads import complete_object_upload

    s3 = FakeS3()
    material_id = start(upload_owner, s3).files[0].material_id
    complete_object_upload(
        database_engine=engine,
        context=upload_owner,
        material_id=material_id,
        parts=part(),
        s3=s3,
        bucket="private",
    )
    content = b"x" * 100
    body = io.BytesIO(content)

    def get_object(**values):
        with Session(engine) as session, session.begin():
            session.connection().exec_driver_sql("SET LOCAL lock_timeout = '250ms'")
            session.exec(
                select(MaterialFile)
                .where(MaterialFile.id == material_id)
                .with_for_update()
            ).one()
        return {**s3.objects[values["Key"]], "Body": body}

    s3.get_object = get_object
    with open_original(
        database_engine=engine,
        context=upload_owner,
        bc_id="bc-a",
        material_id=material_id,
        s3=s3,
        bucket="private",
    ) as original:
        path = Path(original.path)
        assert path.read_bytes() == content and original.byte_size == 100
        assert original.md5 == hashlib.md5(content).hexdigest()
        assert original.sha256 == hashlib.sha256(content).hexdigest()
        assert path.parent.stat().st_mode & 0o777 == 0o700
    assert not path.exists() and body.closed


def test_open_original_rejects_other_bc_before_storage(upload_owner):
    from app.modules.materials.storage import open_original

    s3 = FakeS3()
    material_id = start(upload_owner, s3).files[0].material_id
    count = len(s3.calls)
    with pytest.raises(DomainError):
        with open_original(
            database_engine=engine,
            context=upload_owner,
            bc_id="other-bc",
            material_id=material_id,
            s3=s3,
            bucket="private",
        ):
            pytest.fail("foreign scope yielded")
    assert len(s3.calls) == count


def test_platform_block_preserves_completed_task_and_safe_batch_state(upload_owner):
    from app.modules.materials.uploads import (
        complete_object_upload,
        get_upload_batch,
        refresh_upload_batch,
    )

    s3 = FakeS3()
    batch = start(upload_owner, s3)
    material_id = batch.files[0].material_id
    args = {
        "database_engine": engine,
        "context": upload_owner,
        "material_id": material_id,
        "parts": part(),
        "s3": s3,
        "bucket": "private",
    }
    task = complete_object_upload(**args)
    with Session(engine) as session, session.begin():
        row = session.exec(
            select(ObjectUpload).where(ObjectUpload.material_id == material_id)
        ).one()
        row.status, row.error_code = "blocked", "no_upload_account"
        refresh_upload_batch(
            session, tenant_id=upload_owner.tenant_id, batch_id=batch.batch_id
        )
    assert complete_object_upload(**args) == task
    with Session(engine) as session:
        result = get_upload_batch(
            session, context=upload_owner, batch_id=batch.batch_id
        )
        assert (
            result.status == "blocked"
            and result.files[0].error_code == "no_upload_account"
        )
        # The original stays complete; retry resumes upload to an authorized account.
        assert result.files[0].received_bytes == 100 and result.files[0].can_retry
    with Session(engine) as session, session.begin():
        row = session.exec(
            select(ObjectUpload).where(ObjectUpload.material_id == material_id)
        ).one()
        row.error_code = "arbitrary-credential-value"
    with Session(engine) as session:
        assert (
            "arbitrary-credential-value"
            not in get_upload_batch(
                session, context=upload_owner, batch_id=batch.batch_id
            ).model_dump_json()
        )


@pytest.mark.parametrize("parts", [[], [(1, "a"), (1, "b")], [(2, "a")]])
def test_invalid_completion_parts_never_send(upload_owner, parts):
    from app.modules.materials.schemas import UploadedPart
    from app.modules.materials.uploads import complete_object_upload

    s3 = FakeS3()
    material_id = start(upload_owner, s3).files[0].material_id
    count = len(s3.calls)
    with pytest.raises(DomainError) as error:
        complete_object_upload(
            database_engine=engine,
            context=upload_owner,
            material_id=material_id,
            parts=[UploadedPart(part_number=n, etag=tag) for n, tag in parts],
            s3=s3,
            bucket="private",
        )
    assert error.value.code == "invalid_part" and len(s3.calls) == count


def test_expired_completion_response_does_not_promote_and_later_head_recovers(
    upload_owner,
):
    from app.modules.materials.uploads import complete_object_upload

    s3 = FakeS3()
    material_id = start(upload_owner, s3).files[0].material_id

    def expire(name, _values):
        if name == "head":
            with Session(engine) as session, session.begin():
                row = session.exec(
                    select(ObjectUpload).where(ObjectUpload.material_id == material_id)
                ).one()
                row.claimed_until = datetime.now(UTC) - timedelta(seconds=1)

    s3.callback = expire
    args = {
        "database_engine": engine,
        "context": upload_owner,
        "material_id": material_id,
        "parts": part(),
        "s3": s3,
        "bucket": "private",
    }
    with pytest.raises(DomainError) as error:
        complete_object_upload(**args)
    assert error.value.code == "upload_in_progress"
    with Session(engine) as session:
        assert session.get(MaterialFile, material_id).storage_state == "receiving"
    s3.callback = None
    assert complete_object_upload(**args)
    assert [name for name, _ in s3.calls].count("complete") == 1


def test_cross_tenant_sign_complete_and_original_are_denied(upload_owner, monkeypatch):
    from app.modules.materials.storage import open_original
    from app.modules.materials.uploads import complete_object_upload, sign_upload_part

    fixture = globals()["upload_owner"].__wrapped__(monkeypatch)
    other = next(fixture)
    try:
        s3 = FakeS3()
        material_id = start(upload_owner, s3).files[0].material_id
        count = len(s3.calls)
        for operation in (
            lambda: sign_upload_part(
                database_engine=engine,
                context=other,
                material_id=material_id,
                part_number=1,
                s3=s3,
                bucket="private",
            ),
            lambda: complete_object_upload(
                database_engine=engine,
                context=other,
                material_id=material_id,
                parts=part(),
                s3=s3,
                bucket="private",
            ),
        ):
            with pytest.raises(DomainError) as error:
                operation()
            assert error.value.code == "material_not_found"
        with pytest.raises(DomainError):
            with open_original(
                database_engine=engine,
                context=other,
                bc_id="bc-a",
                material_id=material_id,
                s3=s3,
                bucket="private",
            ):
                pytest.fail("foreign original returned")
        assert len(s3.calls) == count
    finally:
        with pytest.raises(StopIteration):
            next(fixture)


def test_missing_storage_config_does_not_claim_before_send(upload_owner, monkeypatch):
    from app.core.config import settings
    from app.modules.materials.schemas import UploadFileRequest
    from app.modules.materials.uploads import (
        initialize_object_upload,
        start_upload_batch,
    )

    with Session(engine) as session, session.begin():
        batch = start_upload_batch(
            session,
            context=upload_owner,
            bc_id="bc-a",
            request_id=uuid4(),
            files=[
                UploadFileRequest(file_name="Moon.mp4", size=100, mime_type="video/mp4")
            ],
        )
    monkeypatch.setattr(settings, "S3_BUCKET", "")
    with pytest.raises(DomainError) as error:
        initialize_object_upload(
            database_engine=engine,
            context=upload_owner,
            material_id=batch.files[0].material_id,
        )
    assert error.value.code == "object_storage_unconfigured"
    with Session(engine) as session:
        row = session.exec(
            select(ObjectUpload).where(
                ObjectUpload.material_id == batch.files[0].material_id
            )
        ).one()
        assert row.status == "pending" and row.attempt_token is None


def test_retry_only_accepts_explicit_object_failure(upload_owner):
    from app.modules.materials.uploads import (
        complete_object_upload,
        retry_object_upload,
    )

    s3 = FakeS3()
    batch = start(upload_owner, s3)
    material_id = batch.files[0].material_id
    with pytest.raises(DomainError) as error:
        retry_object_upload(
            database_engine=engine,
            context=upload_owner,
            material_id=material_id,
            s3=s3,
            bucket="private",
        )
    assert error.value.code == "upload_not_retryable"
    s3.complete_mode = "invalid"
    with pytest.raises(DomainError):
        complete_object_upload(
            database_engine=engine,
            context=upload_owner,
            material_id=material_id,
            parts=part(),
            s3=s3,
            bucket="private",
        )
    result = retry_object_upload(
        database_engine=engine,
        context=upload_owner,
        material_id=material_id,
        s3=s3,
        bucket="private",
    )
    assert result.status == "receiving" and result.can_retry
    s3.complete_mode = "ok"
    complete_object_upload(
        database_engine=engine,
        context=upload_owner,
        material_id=material_id,
        parts=part(),
        s3=s3,
        bucket="private",
    )
    with pytest.raises(DomainError):
        retry_object_upload(
            database_engine=engine,
            context=upload_owner,
            material_id=material_id,
            s3=s3,
            bucket="private",
        )


def test_real_s3_presign_is_exact_private_scope_and_has_no_wire_logs(
    monkeypatch, caplog
):
    import logging
    from urllib.parse import parse_qs, urlsplit

    from app.core.config import settings
    from app.modules.materials.storage import make_s3, sign_part

    for name, value in {
        "S3_BUCKET": "private",
        "S3_ENDPOINT_URL": "https://storage.invalid",
        "S3_ACCESS_KEY_ID": "fixture-access",
        "S3_SECRET_ACCESS_KEY": "fixture-secret",
    }.items():
        monkeypatch.setattr(settings, name, value)
    with caplog.at_level(logging.DEBUG):
        client = make_s3()
        try:
            url = sign_part(
                client,
                bucket="private",
                key="tenants/fake/materials/file/original",
                upload_id="fixture-upload",
                part_number=2,
            )
        finally:
            client.close()
    query = parse_qs(urlsplit(url).query)
    assert query["X-Amz-Expires"] == ["900"] and query["partNumber"] == ["2"]
    assert query["uploadId"] == ["fixture-upload"]
    assert urlsplit(url).path == "/private/tenants/fake/materials/file/original"
    assert "fixture-secret" not in url + caplog.text
    assert "X-Amz-Signature" not in caplog.text


def test_boto_stubber_validates_private_multipart_and_completion_transport(
    upload_owner,
):
    import boto3
    from botocore.stub import Stubber

    from app.modules.materials.schemas import UploadFileRequest
    from app.modules.materials.storage import object_key_for
    from app.modules.materials.uploads import (
        complete_object_upload,
        initialize_object_upload,
        start_upload_batch,
    )

    with Session(engine) as session, session.begin():
        batch = start_upload_batch(
            session,
            context=upload_owner,
            bc_id="bc-a",
            request_id=uuid4(),
            files=[
                UploadFileRequest(file_name="Moon.mp4", size=100, mime_type="video/mp4")
            ],
        )
    material = batch.files[0].material_id
    key = object_key_for(upload_owner.tenant_id, material)
    metadata = {
        "tenant-id": str(upload_owner.tenant_id),
        "material-id": str(material),
        "byte-size": "100",
    }
    client = boto3.client(
        "s3",
        region_name="us-east-1",
        endpoint_url="https://storage.invalid",
        aws_access_key_id="fixture",
        aws_secret_access_key="fixture",
    )
    with Stubber(client) as stub:
        stub.add_response(
            "create_multipart_upload",
            {"UploadId": "fake-multipart"},
            {
                "Bucket": "private",
                "Key": key,
                "ACL": "private",
                "ContentType": "video/mp4",
                "Metadata": metadata,
            },
        )
        stub.add_response(
            "complete_multipart_upload",
            {"ETag": "multipart-not-md5"},
            {
                "Bucket": "private",
                "Key": key,
                "UploadId": "fake-multipart",
                "MultipartUpload": {
                    "Parts": [{"PartNumber": 1, "ETag": '"fake-etag"'}]
                },
            },
        )
        stub.add_response(
            "head_object",
            {"ContentLength": 100, "Metadata": metadata},
            {"Bucket": "private", "Key": key},
        )
        initialize_object_upload(
            database_engine=engine,
            context=upload_owner,
            material_id=material,
            s3=client,
            bucket="private",
        )
        assert complete_object_upload(
            database_engine=engine,
            context=upload_owner,
            material_id=material,
            parts=part(),
            s3=client,
            bucket="private",
        )
        stub.assert_no_pending_responses()
    client.close()


def test_batch_progress_uses_latest_real_attempt_and_preserves_source(upload_owner):
    from app.modules.materials.models import (
        MaterialAssetOperation,
        MaterialUploadAttempt,
    )
    from app.modules.materials.uploads import (
        complete_object_upload,
        get_upload_batch,
        refresh_upload_batch,
    )
    from tests.modules.materials.test_tenant_materials import mapping

    s3 = FakeS3()
    batch = start(upload_owner, s3)
    material_id = batch.files[0].material_id
    complete_object_upload(
        database_engine=engine,
        context=upload_owner,
        material_id=material_id,
        parts=part(),
        s3=s3,
        bucket="private",
    )
    with Session(engine) as session, session.begin():
        file = session.get(MaterialFile, material_id)
        asset = mapping(session, upload_owner, file)
        operation = MaterialAssetOperation(
            tenant_id=upload_owner.tenant_id,
            bc_id="bc-a",
            material_id=material_id,
            advertiser_id=asset.advertiser_id,
            path="upload_original",
            status="failed",
            request_digest="a" * 64,
        )
        session.add(operation)
        session.flush()
        attempt = MaterialUploadAttempt(
            tenant_id=upload_owner.tenant_id,
            bc_id="bc-a",
            material_id=material_id,
            advertiser_id=asset.advertiser_id,
            connection_id=asset.connection_id,
            operation_id=operation.id,
            status="uploading",
            request_digest="a" * 64,
        )
        session.add(attempt)
        refresh_upload_batch(
            session, tenant_id=upload_owner.tenant_id, batch_id=batch.batch_id
        )
    with Session(engine) as session:
        result = get_upload_batch(
            session, context=upload_owner, batch_id=batch.batch_id
        )
        assert (
            result.status == "uploading"
            and result.files[0].latest_advertiser_id == "account-a"
        )
        assert session.get(UploadBatch, batch.batch_id).status == "uploading"
        assert result.files[0].received_bytes == 100 and not result.files[0].can_retry


def test_original_context_preserves_consumer_failure_and_cleans_body(upload_owner):
    import io

    from app.modules.materials.storage import open_original
    from app.modules.materials.uploads import complete_object_upload

    s3 = FakeS3()
    material_id = start(upload_owner, s3).files[0].material_id
    complete_object_upload(
        database_engine=engine,
        context=upload_owner,
        material_id=material_id,
        parts=part(),
        s3=s3,
        bucket="private",
    )
    stream = io.BytesIO(b"x" * 100)
    s3.get_object = lambda **values: {**s3.objects[values["Key"]], "Body": stream}
    failure = OSError("consumer failure")
    with pytest.raises(OSError) as error:
        with open_original(
            database_engine=engine,
            context=upload_owner,
            bc_id="bc-a",
            material_id=material_id,
            s3=s3,
            bucket="private",
        ):
            raise failure
    assert error.value is failure and stream.closed


def test_invalid_first_part_does_not_initialize_remote_session(upload_owner):
    from app.modules.materials.schemas import UploadFileRequest
    from app.modules.materials.uploads import sign_upload_part, start_upload_batch

    with Session(engine) as session, session.begin():
        batch = start_upload_batch(
            session,
            context=upload_owner,
            bc_id="bc-a",
            request_id=uuid4(),
            files=[
                UploadFileRequest(file_name="Moon.mp4", size=100, mime_type="video/mp4")
            ],
        )
    s3 = FakeS3()
    with pytest.raises(DomainError) as error:
        sign_upload_part(
            database_engine=engine,
            context=upload_owner,
            material_id=batch.files[0].material_id,
            part_number=2,
            s3=s3,
            bucket="private",
        )
    assert error.value.code == "invalid_part" and not s3.calls
