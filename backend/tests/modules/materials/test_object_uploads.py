from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import inspect

from app.core.db import engine
from app.modules.materials.models import ObjectUpload


def test_object_upload_has_durable_network_claim():
    token = uuid4()
    expiry = datetime.now(UTC)
    row = ObjectUpload(attempt_token=token, claimed_until=expiry)
    assert row.attempt_token == token
    assert row.claimed_until == expiry
    columns = {
        column["name"] for column in inspect(engine).get_columns("object_upload")
    }
    assert {"attempt_token", "claimed_until"} <= columns
