"""失败证据保留脱敏说明、冻结范围和封面来源状态。"""

from uuid import uuid4

from sqlmodel import Session, select

from app.core.db import engine
from app.modules.builds.execution_models import StepEvidence
from tests.modules.builds.test_execution_state import attempt as attempt
from tests.modules.builds.test_previews import prepared as prepared
from tests.modules.materials.test_cover_throughput import matrix
from tests.modules.materials.test_source_uploads import source_env as source_env


def test_local_failure_persists_message_and_emits_correlatable_event(
    session, attempt, monkeypatch
):
    from app.modules.builds import execution_state as state

    step, claim = attempt
    events = []
    monkeypatch.setattr(
        state, "emit", lambda event, **fields: events.append((event, fields))
    )
    state.finish_local(
        session,
        claim=claim,
        status="FAILED",
        code="cover_source_unavailable",
        local_message="来源封面不可用 https://private.example/a?token=secret",
    )
    row = session.exec(
        select(StepEvidence).where(StepEvidence.step_id == step.id)
    ).one()
    assert row.summary["local_message"] == "来源封面不可用 [REDACTED]"
    assert row.summary["failure_context"]["bc_id"] == claim.bc_id
    event = next(fields for name, fields in events if name == "build_failure_recorded")
    assert event["evidence_id"] == str(row.id)
    assert event["submission_id"] == str(claim.submission_id)
    assert "secret" not in str(events)


def test_cover_diagnostics_identify_unknown_source_and_scope(source_env):
    from app.modules.builds.failure_diagnostics import cover_failure_context
    from app.modules.materials.cover_models import MaterialCoverJob

    target_id = matrix(source_env, 1, 1)[0]
    with Session(engine) as db, db.begin():
        target = db.get(MaterialCoverJob, target_id)
        source = db.exec(
            select(MaterialCoverJob).where(
                MaterialCoverJob.tenant_id == target.tenant_id,
                MaterialCoverJob.material_id == target.material_id,
                MaterialCoverJob.purpose == "SOURCE",
            )
        ).one()
        source.status, source.error_code = "UNKNOWN", "cover_result_unknown"
        target.status, target.error_code = "BLOCKED", "cover_source_unavailable"
        db.flush()
        result = cover_failure_context(
            db,
            tenant_id=target.tenant_id,
            bc_id=target.bc_id,
            advertiser_id=target.advertiser_id,
            connection_id=target.connection_id,
            material_ids=[target.material_id],
        )
        assert result["targets"][0]["job_id"] == str(target.id)
        assert result["sources"][0]["error_code"] == "cover_result_unknown"
        assert result["sources"][0]["job_id"] == str(source.id)
        assert (
            cover_failure_context(
                db,
                tenant_id=uuid4(),
                bc_id=target.bc_id,
                advertiser_id=target.advertiser_id,
                connection_id=target.connection_id,
                material_ids=[target.material_id],
            )["targets"]
            == []
        )
