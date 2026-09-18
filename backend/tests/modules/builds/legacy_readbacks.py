"""显式重建旧版本的自动核查图，仅供历史恢复回归用例。"""

from sqlmodel import select

from app.modules.builds.execution_models import ExecutionStep


def add_legacy_readbacks(session, submission_id):
    sources = session.exec(
        select(ExecutionStep).where(
            ExecutionStep.submission_id == submission_id,
            ExecutionStep.kind.in_(["CAMPAIGN", "ADGROUP", "AD"]),
        )
    ).all()
    for source in sources:
        session.add(
            ExecutionStep(
                tenant_id=source.tenant_id,
                submission_id=source.submission_id,
                preview_id=source.preview_id,
                bc_id=source.bc_id,
                unit_id=source.unit_id,
                kind="READBACK",
                step_key=f"{source.unit_id}:READBACK:{source.step_key.split(':', 1)[1]}",
                parent_step_id=source.id,
                group_id=source.group_id,
                planned_ad_id=source.planned_ad_id,
            )
        )
    session.flush()
