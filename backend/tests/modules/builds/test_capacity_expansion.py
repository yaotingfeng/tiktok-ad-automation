"""A bounded step page is one SQL insertion, with real parent FKs and rollback."""

from uuid import uuid4

import pytest
from sqlalchemy import event, func
from sqlmodel import select

from app.modules.builds import submissions
from app.modules.builds.execution_models import ExecutionStep
from tests.modules.builds.test_submissions import frozen as frozen
from tests.modules.builds.test_submissions import prepared as prepared


def test_bounded_expansion_inserts_one_statement_per_page_and_rolls_back(
    session, context, frozen
):
    receipt = submissions.submit_preview(
        session, context=context, preview_id=frozen, request_id=uuid4()
    )
    submissions.expand_submission(
        session, context=context, submission_id=receipt.submission_id, limit=1
    )
    statements = []
    connection = session.connection()

    def record(_c, _cur, statement, _p, _ctx, _many):
        if statement.startswith("INSERT INTO execution_step"):
            statements.append(statement)

    event.listen(connection, "before_cursor_execute", record)
    try:
        with pytest.raises(RuntimeError, match="lost page"), session.begin_nested():
            submissions.expand_submission(
                session, context=context, submission_id=receipt.submission_id, limit=35
            )
            assert (
                session.exec(select(func.count()).select_from(ExecutionStep)).one()
                == 35
            )
            raise RuntimeError("lost page")
        assert session.exec(select(func.count()).select_from(ExecutionStep)).one() == 0
        assert len(statements) == 1
        statements.clear()
        submissions.expand_submission(
            session, context=context, submission_id=receipt.submission_id, limit=35
        )
        assert len(statements) == 1
        assert session.exec(select(func.count()).select_from(ExecutionStep)).one() == 35
    finally:
        event.remove(connection, "before_cursor_execute", record)
    # Remaining pages cross group/ad/readback parent boundaries. PostgreSQL checks
    # self-reference FKs for every actual insert; a replay cannot duplicate steps.
    while not submissions.expand_submission(
        session, context=context, submission_id=receipt.submission_id
    ):
        pass
    before = session.exec(select(func.count()).select_from(ExecutionStep)).one()
    assert before == 264
    assert submissions.expand_submission(
        session, context=context, submission_id=receipt.submission_id
    )
    assert session.exec(select(func.count()).select_from(ExecutionStep)).one() == before


def test_step_pages_reuse_compiled_insert_instead_of_rebuilding_all_parameters(
    session, context, frozen
):
    receipt = submissions.submit_preview(
        session, context=context, preview_id=frozen, request_id=uuid4()
    )
    cache_hits = []

    def record(_c, _cur, statement, _p, execution_context, _many):
        if statement.startswith("INSERT INTO execution_step"):
            cache_hits.append(execution_context.cache_hit.name)

    connection = session.connection()
    event.listen(connection, "before_cursor_execute", record)
    try:
        for _ in range(4):
            submissions.expand_submission(
                session, context=context, submission_id=receipt.submission_id, limit=10
            )
    finally:
        event.remove(connection, "before_cursor_execute", record)
    assert len(cache_hits) == 4 and "CACHE_HIT" in cache_hits
