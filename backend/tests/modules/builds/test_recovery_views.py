"""新授权只读资格属于具体步骤，不会开放原提交重试。"""

from sqlmodel import Session

from app.modules.builds.execution_models import ExecutionStep
from app.modules.builds.submissions import get_submission, get_submission_steps
from app.modules.tenants.models import TenantMembership
from tests.modules.builds.test_historical_read import renewed
from tests.modules.builds.test_reconciliation import arm
from tests.modules.builds.test_reconciliation import recon_env as recon_env


def test_reauthorized_projection_exposes_specific_step_without_resuming_submission(
    recon_env,
):
    env = recon_env
    step_id, _ = arm(env)
    renewed(env, step_id)
    with Session(env.engine) as session:
        source = session.get(ExecutionStep, step_id)
        result = get_submission(
            session, context=env.context, submission_id=source.submission_id
        )
        assert result.recovery_mode == "REAUTHORIZE_READ"
        assert not result.recovery.can_retry and not result.recovery.can_reconcile
        page = get_submission_steps(
            session,
            context=env.context,
            submission_id=source.submission_id,
            kind="CAMPAIGN",
        )
        # 夹具含数百素材步骤；按本次核查种类读完整六个组合，不能假定随机 UUID 在首50条。
        assert len(page.items) == 6 and page.next_cursor is None
        assert {item.step_id for item in page.items if item.can_historical_read} == {
            step_id
        }
        member = session.get(
            TenantMembership, (env.context.tenant_id, env.context.actor_id)
        )
        member.role = "viewer"
        session.commit()
        result = get_submission(
            session, context=env.context, submission_id=source.submission_id
        )
        assert result.recovery_mode == "BLOCKED"
        page = get_submission_steps(
            session,
            context=env.context,
            submission_id=source.submission_id,
            kind="CAMPAIGN",
        )
        assert not any(item.can_historical_read for item in page.items)
