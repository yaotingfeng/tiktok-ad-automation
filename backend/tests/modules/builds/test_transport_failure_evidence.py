"""创建失败的安全网络摘要必须随已有事实持久保存，不能依赖旧Pod日志。"""

import pytest
import urllib3
from sqlmodel import Session, select

from app.modules.builds.execution_models import ExecutionStep, StepEvidence
from tests.modules.builds.test_execution import executable as executable
from tests.modules.builds.test_execution import run


@pytest.mark.parametrize(
    "phase,conclusion,status",
    [
        ("dns_tcp_connect", "NOT_SENT", "PENDING"),
        ("response_read", "RESULT_UNKNOWN", "UNKNOWN"),
    ],
)
def test_network_failure_summary_survives_new_session(
    executable, redis_client, monkeypatch, phase, conclusion, status
):
    def failed(*_args, **_kwargs):
        raise TimeoutError("private-token must never enter persisted evidence")

    if phase == "dns_tcp_connect":
        monkeypatch.setattr(urllib3.connection.HTTPSConnection, "_new_conn", failed)
    else:
        monkeypatch.setattr(
            urllib3.HTTPSConnectionPool, "_validate_conn", lambda *_args: None
        )
        monkeypatch.setattr(
            urllib3.connection.HTTPSConnection,
            "request",
            lambda *_args, **_kwargs: None,
        )
        monkeypatch.setattr(urllib3.connection.HTTPSConnection, "getresponse", failed)
    run(executable, redis_client, "CTA")
    engine, _, ids = executable
    with Session(engine) as db:
        step = db.get(ExecutionStep, ids["CTA"][0])
        assert step.status == status
        saved = db.exec(
            select(StepEvidence).where(
                StepEvidence.step_id == step.id, StepEvidence.conclusion == conclusion
            )
        ).one()
        assert saved.summary["transport"]["failure_stage"] == phase
        assert saved.summary["transport"]["phase_ms"][phase] >= 0
        assert "private-token" not in str(saved.summary)
