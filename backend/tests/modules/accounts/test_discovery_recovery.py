from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlmodel import Session, col, select

from app.core.db import engine
from app.core.errors import DomainError
from app.jobs.models import PendingDispatch
from app.modules.accounts import tasks
from app.modules.accounts.discovery_recovery import (
    LOCAL_RESOURCE_ERROR,
    LOCAL_RETRY_LIMIT,
    _requeue,
    repair_discoveries,
)
from app.modules.accounts.models import DiscoveryRun, TikTokConnection
from app.modules.accounts.runtime_directory import MODE as RUNTIME_MODE
from app.modules.accounts.runtime_directory import TASK_NAME as RUNTIME_TASK
from tests.modules.accounts.test_discovery_worker import (  # noqa: F401
    committed_runs,
    directory_transport,
    step,
)


def _set(run_id, **values):
    with Session(engine) as session:
        run = session.get(DiscoveryRun, run_id)
        for key, value in values.items():
            setattr(run, key, value)
        session.add(run)
        session.commit()


def _repair_keys(run_id):
    with Session(engine) as session:
        return [
            d.task_key
            for d in session.exec(
                select(PendingDispatch).where(
                    col(PendingDispatch.task_key).contains(f"{run_id}:"),
                    col(PendingDispatch.task_key).contains(":repair:"),
                )
            ).all()
        ]


def _repair_only(run_ids):
    # 其他用例残留的活动任务不应影响本用例：只看本用例的任务是否被补派。
    repair_discoveries(database_engine=engine)
    return {run_id: _repair_keys(run_id) for run_id in run_ids}


def test_lost_step_message_is_redispatched_and_run_completes(
    committed_runs,  # noqa: F811
    directory_transport,  # noqa: ARG001, F811
    redis_client,
):
    contexts, runs = committed_runs
    step(contexts[0], runs[0], redis_client)
    # 模拟这一步的派发消息丢失：任务已到期很久，没有任何领取。
    _set(runs[0], next_attempt_at=datetime.now(UTC) - timedelta(minutes=5))
    keys = _repair_only([runs[0]])[runs[0]]
    assert len(keys) == 1 and keys[0].startswith(f"discover:{runs[0]}:")
    # 宽限期内不重复补派。
    assert _repair_only([runs[0]])[runs[0]] == keys
    for _ in range(6):
        step(contexts[0], runs[0], redis_client)
    with Session(engine) as session:
        assert session.get(DiscoveryRun, runs[0]).status == "COMPLETE"


def test_live_claim_recent_step_and_finished_runs_are_left_alone(
    committed_runs,  # noqa: F811
):
    _contexts, runs = committed_runs
    now = datetime.now(UTC)
    _set(
        runs[0],
        next_attempt_at=now - timedelta(minutes=5),
        claim_id=uuid4(),
        claimed_until=now + timedelta(seconds=30),
    )
    _set(runs[1], next_attempt_at=now - timedelta(seconds=10))
    assert _repair_only(runs) == {runs[0]: [], runs[1]: []}
    _set(runs[1], status="ERROR", next_attempt_at=now - timedelta(minutes=5))
    assert _repair_only(runs) == {runs[0]: [], runs[1]: []}
    # 领取已过期且原恢复消息也没处理时才补派。
    _set(runs[0], claimed_until=now - timedelta(minutes=5))
    assert len(_repair_only(runs)[runs[0]]) == 1


def test_repeated_local_resource_failures_back_off_then_fail_visibly(
    committed_runs,  # noqa: F811
    directory_transport,  # noqa: ARG001, F811
    redis_client,
    monkeypatch,
):
    contexts, runs = committed_runs

    def unavailable(*_args, **_kwargs):
        raise DomainError(LOCAL_RESOURCE_ERROR, "调用前资源暂不可用", retryable=True)

    monkeypatch.setattr(tasks, "read_discovery_stage", unavailable)
    delays = []
    for _ in range(LOCAL_RETRY_LIMIT + 1):
        _set(runs[0], next_attempt_at=None)
        before = datetime.now(UTC)
        step(contexts[0], runs[0], redis_client)
        with Session(engine) as session:
            run = session.get(DiscoveryRun, runs[0])
            if run.status == "ADMISSION_WAIT":
                delays.append((run.next_attempt_at - before).total_seconds())
    assert len(delays) == LOCAL_RETRY_LIMIT
    assert delays[0] < delays[1] < delays[2] and max(delays) <= 301
    with Session(engine) as session:
        run = session.get(DiscoveryRun, runs[0])
        assert (run.status, run.error_code) == ("ERROR", LOCAL_RESOURCE_ERROR)
    # 已落 ERROR 的任务不再被兜底补派。
    _set(runs[0], next_attempt_at=datetime.now(UTC) - timedelta(minutes=5))
    assert _repair_only([runs[0]])[runs[0]] == []


def test_successful_step_clears_local_failure_count(
    committed_runs,  # noqa: F811
    directory_transport,  # noqa: ARG001, F811
    redis_client,
    monkeypatch,
):
    contexts, runs = committed_runs
    original = tasks.read_discovery_stage
    failing = {"left": 2}

    def flaky(*args, **kwargs):
        if failing["left"]:
            failing["left"] -= 1
            raise DomainError(
                LOCAL_RESOURCE_ERROR, "调用前资源暂不可用", retryable=True
            )
        return original(*args, **kwargs)

    monkeypatch.setattr(tasks, "read_discovery_stage", flaky)
    for _ in range(2):
        _set(runs[0], next_attempt_at=None)
        step(contexts[0], runs[0], redis_client)
    with Session(engine) as session:
        assert session.get(DiscoveryRun, runs[0]).work["local_failures"] == 2
    _set(runs[0], next_attempt_at=None)
    step(contexts[0], runs[0], redis_client)
    with Session(engine) as session:
        run = session.get(DiscoveryRun, runs[0])
        assert run.status == "RUNNING" and "local_failures" not in run.work


def test_requeue_uses_each_run_kind_own_consumer(committed_runs):  # noqa: F811
    contexts, runs = committed_runs
    with Session(engine) as session:
        api_run = session.get(DiscoveryRun, runs[0])
        mcp_connection = TikTokConnection(
            tenant_id=contexts[0].tenant_id, kind="OFFICIAL_MCP", status="ACTIVE"
        )
        session.add(mcp_connection)
        session.flush()
        mcp_run = DiscoveryRun(
            tenant_id=contexts[0].tenant_id,
            actor_id=contexts[0].actor_id,
            connection_id=mcp_connection.id,
            bc_id=f"bc-{uuid4()}",
            work={"stage": "BCS", "page": 1},
        )
        runtime_run = DiscoveryRun(
            tenant_id=contexts[0].tenant_id,
            actor_id=contexts[0].actor_id,
            connection_id=api_run.connection_id,
            bc_id=f"bc-{uuid4()}",
            work={"mode": RUNTIME_MODE, "stage": "ASSETS", "page": 1},
        )
        session.add_all([mcp_run, runtime_run])
        session.flush()
        expected = {
            api_run.id: "accounts.discover",
            mcp_run.id: "accounts.mcp_discover",
            runtime_run.id: RUNTIME_TASK,
        }
        for run in (api_run, mcp_run, runtime_run):
            _requeue(session, run, suffix=f"repair:{uuid4()}")
        session.flush()
        for run_id, task_name in expected.items():
            names = session.exec(
                select(PendingDispatch.task_name).where(
                    col(PendingDispatch.task_key).contains(f"{run_id}:"),
                    col(PendingDispatch.task_key).contains(":repair:"),
                )
            ).all()
            assert names == [task_name]
        session.rollback()
