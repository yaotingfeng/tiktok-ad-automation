"""Bounded material evidence shares authority reads, never remote state or writes."""

from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session as SASession
from sqlmodel import Session

from app.core.db import engine
from app.core.errors import DomainError
from app.modules.materials import readiness
from app.modules.materials.models import MaterialFile
from tests.modules.materials.test_readiness import (
    asset,
    target,
)
from tests.modules.materials.test_readiness import (
    runtime_config as runtime_config,
)
from tests.modules.materials.test_readiness import (
    source_env as source_env,
)
from tests.modules.materials.test_readiness import (
    wire as wire,
)


def test_batch_readiness_is_bounded_read_only_and_does_not_repeat_authority(
    source_env, wire
):
    with Session(engine) as session, session.begin():
        account = target(session, source_env)
        asset(session, source_env, account)
        original = session.get(MaterialFile, source_env["material_id"])
        ids = [original.id]
        for number in range(29):
            identity = uuid4()
            row = MaterialFile(
                **(
                    original.model_dump()
                    | {
                        "id": identity,
                        "object_key": f"batch/{identity}",
                        "storage_state": "stored" if number % 2 else "unavailable",
                    }
                )
            )
            session.add(row)
            ids.append(identity)
    statements = []
    with Session(engine) as session, session.begin():
        SASession.execute(session, text("SET TRANSACTION READ ONLY"))
        connection = session.connection()

        def record(_c, _cu, statement, _p, _ct, _many):
            statements.append(statement)

        event.listen(connection, "before_cursor_execute", record)
        try:
            result = readiness.get_material_readiness_batch(
                session,
                context=source_env["context"],
                bc_id=source_env["bc_id"],
                material_ids=ids,
                advertiser_id=account,
            )
        finally:
            event.remove(connection, "before_cursor_execute", record)
        assert len(statements) <= 24
        assert all(
            statement.lstrip().upper().startswith("SELECT") for statement in statements
        )
        assert not session.new and not session.dirty
        assert result[ids[0]].state == "ready"
        for number, identity in enumerate(ids[1:]):
            assert result[identity].state == ("preparable" if number % 2 else "blocked")
        for identity in ids:
            assert (
                readiness.get_material_readiness(
                    session,
                    context=source_env["context"],
                    bc_id=source_env["bc_id"],
                    material_id=identity,
                    advertiser_id=account,
                )
                == result[identity]
            )
    assert wire[0] == []


def test_batch_rejects_unbounded_and_missing_materials(source_env, wire):
    with Session(engine) as session:
        with pytest.raises(ValueError):
            readiness.get_material_readiness_batch(
                session,
                context=source_env["context"],
                bc_id=source_env["bc_id"],
                material_ids=[uuid4() for _ in range(51)],
                advertiser_id="actual-account",
            )
        with pytest.raises(DomainError, match="未找到"):
            readiness.get_material_readiness_batch(
                session,
                context=source_env["context"],
                bc_id=source_env["bc_id"],
                material_ids=[source_env["material_id"], uuid4()],
                advertiser_id="actual-account",
            )

    assert wire[0] == []
