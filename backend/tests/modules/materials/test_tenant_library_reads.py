"""Tenant catalogue is independent of destination BC; upload provenance is immutable."""

from uuid import uuid4

from sqlmodel import Session

from app.core.db import engine
from app.modules.accounts.models import TenantBC
from app.modules.materials.models import MaterialFile, UploadBatch
from tests.modules.materials.test_object_uploads import upload_owner as upload_owner
from tests.modules.materials.test_upload_api import api as api


def test_tenant_catalogue_and_details_need_no_selected_bc(api, upload_owner):
    client, storage, path = api
    identity = uuid4()
    with Session(engine) as db, db.begin():
        db.add(TenantBC(tenant_id=upload_owner.tenant_id, bc_id="bc-b"))
    with Session(engine) as db, db.begin():
        db.add(
            MaterialFile(
                id=identity,
                tenant_id=upload_owner.tenant_id,
                bc_id="bc-a",
                file_name="First.mp4",
                object_key=f"offline/{uuid4()}",
                byte_size=10,
            )
        )
        db.add(
            MaterialFile(
                tenant_id=upload_owner.tenant_id,
                bc_id="bc-b",
                file_name="Other.mp4",
                object_key=f"offline/{uuid4()}",
                byte_size=10,
            )
        )
        db.add(
            UploadBatch(
                tenant_id=upload_owner.tenant_id,
                bc_id="bc-b",
                actor_id=upload_owner.actor_id,
                request_id=uuid4(),
                request_digest="a" * 64,
                status="receiving",
            )
        )
    response = client.get(path)
    assert response.status_code == 200
    assert {row["bc_id"] for row in response.json()["items"]} == {"bc-a", "bc-b"}
    for suffix in ("", "/assets", "/attempts"):
        result = client.get(f"{path}/{identity}{suffix}")
        assert result.status_code == 200
    assert client.get(path + "/upload-batches").json()["total"] == 1
    assert not storage.calls


def test_catalogue_deduplicates_content_before_paging_but_search_keeps_alias_names(
    api, upload_owner
):
    client, _, path = api
    from datetime import UTC, datetime

    with Session(engine) as db, db.begin():
        for name in ("Alpha.mp4", "Beta.mp4"):
            db.add(
                MaterialFile(
                    tenant_id=upload_owner.tenant_id,
                    bc_id="bc-a",
                    file_name=name,
                    object_key=f"offline/{uuid4()}",
                    byte_size=10,
                    sha256="a" * 64,
                    video_md5="b" * 32,
                    digest_verified_at=datetime.now(UTC),
                )
            )
    result = client.get(path, params={"limit": 1})
    assert result.status_code == 200
    assert result.json()["total"] == 1
    assert result.json()["next_cursor"] is None
    assert result.json()["items"][0]["file_name"] == "Alpha.mp4"
    aliases = client.get(path, params={"query": "Beta"}).json()
    assert aliases["total"] == 1
    assert aliases["items"][0]["file_name"] == "Beta.mp4"
