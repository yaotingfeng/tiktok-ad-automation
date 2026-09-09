"""Preserve outcome precedence while removing a per-object full-group scan."""

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session as SASession
from sqlmodel import select

from app.modules.builds import submission_catalog, submissions
from app.modules.builds.execution_models import ExecutionStep, Submission
from tests.modules.builds.test_previews import prepared as prepared
from tests.modules.builds.test_submissions import frozen as frozen


@pytest.mark.parametrize(
    "case",
    [
        "before",
        "partial",
        "pending",
        "material_one",
        "material_all",
        "CTA",
        "CAMPAIGN",
        "ADGROUP",
        "precedence",
    ],
)
def test_summary_outcomes_equal_frozen_legacy_oracle(
    session, context, frozen, case, monkeypatch
):
    identity = submissions.submit_preview(
        session, context=context, preview_id=frozen, request_id=uuid4()
    ).submission_id
    if case == "partial":
        submissions.expand_submission(
            session, context=context, submission_id=identity, limit=5
        )
    elif case != "before":
        while not submissions.expand_submission(
            session, context=context, submission_id=identity
        ):
            pass
    rows = session.exec(select(ExecutionStep).order_by(ExecutionStep.step_key)).all()
    if case in {"CTA", "CAMPAIGN", "ADGROUP"}:
        row = next(row for row in rows if row.kind == case)
        row.status = "FAILED"
        session.add(row)
    elif case.startswith("material_"):
        first = next(row for row in rows if row.kind == "MATERIAL")
        for row in rows:
            if (
                row.kind == "MATERIAL"
                and row.unit_id == first.unit_id
                and (case == "material_all" or row.id == first.id)
            ):
                row.status = "FAILED"
                session.add(row)
    elif case == "precedence":
        ads = [row for row in rows if row.kind == "AD"][:3]
        for row, status, remote in zip(
            ads,
            ["UNKNOWN", "SUCCEEDED", "FAILED"],
            [None, None, "known-remote"],
            strict=True,
        ):
            row.status, row.remote_id = status, remote
            session.add(row)
        for row in rows:
            if row.kind == "CAMPAIGN":
                row.status = "FAILED"
                session.add(row)
    session.flush()
    query = Path(__file__).with_name("legacy_submission_outcomes.sql").read_text()
    expected = (
        SASession.execute(
            session, text(query), submissions.params(session.get(Submission, identity))
        )
        .mappings()
        .all()
    )
    view = submissions.get_submission(session, context=context, submission_id=identity)
    fields = {
        "CAMPAIGN": "campaign_count",
        "ADGROUP": "adgroup_count",
        "AD": "ad_count",
    }
    for state in ("succeeded", "failed", "unknown", "pending"):
        values = {row["kind"]: row["n"] for row in expected if row["outcome"] == state}
        assert getattr(view, state).model_dump() == {
            field: values.get(kind, 0) for kind, field in fields.items()
        }
    actual = submission_catalog.list_submissions(
        session, context=context, bc_id=view.bc_id
    )
    with monkeypatch.context() as patch:
        patch.setattr(
            submission_catalog,
            "COUNTS",
            Path(__file__).with_name("legacy_catalog_counts.sql").read_text(),
        )
        legacy = submission_catalog.list_submissions(
            session, context=context, bc_id=view.bc_id
        )
    assert actual.model_dump() == legacy.model_dump()


@pytest.mark.parametrize("catalog", [False, True])
def test_summary_plan_never_rescans_all_material_groups_per_object(
    session, context, frozen, catalog
):
    identity = submissions.submit_preview(
        session, context=context, preview_id=frozen, request_id=uuid4()
    ).submission_id
    while not submissions.expand_submission(
        session, context=context, submission_id=identity
    ):
        pass
    captured = []
    connection = session.connection()

    def capture(_conn, _cursor, statement, parameters, _context, _many):
        if (
            "SELECT kind,outcome,count(*)" in statement
            or "result_counts AS (" in statement
        ):
            captured.append((statement, parameters))

    event.listen(connection, "before_cursor_execute", capture)
    try:
        if catalog:
            submission_catalog.list_submissions(
                session, context=context, bc_id="bc-draft"
            )
        else:
            submissions.get_submission(session, context=context, submission_id=identity)
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert len(captured) == 1
    statement, parameters = captured[0]
    plan = connection.exec_driver_sql(
        "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + statement, parameters
    ).scalar_one()[0]["Plan"]

    def nodes(node):
        yield node
        for child in node.get("Plans", []):
            yield from nodes(child)

    # Small fixtures may hash these SubPlans; the 600k-group grand CTE spills
    # and falls back to a scan per object. Do not retain that unstable shape.
    rescans = [
        node
        for node in nodes(plan)
        if node.get("Node Type") == "CTE Scan"
        and node.get("Parent Relationship") == "SubPlan"
    ]
    assert not rescans, "A spilled full-preview CTE must not be scanned for each object"
