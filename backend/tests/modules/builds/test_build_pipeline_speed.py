"""搭建链路提速与自愈：窗口及时让出、广告无锁快路径、封面瞬时失败自动接续。

对应 2026-10-24 生产两批搭建的卡点：广告等待封面时反复加锁准备、封面本地
锁等待超时被判终态、UNKNOWN 封面无人接续、素材完成的组合仍占执行窗口。
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, select
from urllib3.response import HTTPResponse

from app.core.errors import DomainError
from app.jobs.models import PendingDispatch
from app.modules.builds.execution import process_step
from app.modules.builds.execution_models import ExecutionStep
from app.modules.builds.execution_window import window_units
from app.modules.builds.preview_models import BuildUnit
from app.modules.materials import cover_sharing, covers
from app.modules.materials.cover_models import MaterialCoverJob
from tests.modules.builds.test_cover_execution import pending, ready_ad_with_cover_job
from tests.modules.builds.test_execution import executable as executable


def _forbid_locked_preparation(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("ready/waiting AD must not re-run locked preparation")

    monkeypatch.setattr("app.modules.builds.execution.ensure_target_asset", forbidden)
    monkeypatch.setattr("app.modules.materials.covers.ensure_cover", forbidden)


def _ad_wire(monkeypatch):
    writes = []

    def wire(_pool, method, url, **kwargs):
        assert method == "POST" and "/ad/create/" in url
        writes.append(json.loads(kwargs["body"]))
        return HTTPResponse(
            body=json.dumps(
                {"code": 0, "data": {"smart_plus_ad_id": "actual-ad"}}
            ).encode(),
            status=200,
        )

    monkeypatch.setattr("urllib3.PoolManager.request", wire)
    return writes


def _settle_structure(env):
    db, _, ids = env
    with Session(db) as session, session.begin():
        for kind in ["MATERIAL", "CTA", "CAMPAIGN", "ADGROUP"]:
            for identity in ids[kind]:
                step = session.get(ExecutionStep, identity)
                step.status, step.phase, step.dispatch_id = "SUCCEEDED", "DONE", None
                step.lease_token = step.lease_expires_at = None
                if kind != "MATERIAL":
                    step.remote_id = f"existing-{kind}"


def _process(env, redis_client, step_id):
    db, context, _ = env
    return process_step(
        database_engine=db,
        redis_client=redis_client,
        context=context,
        step_id=step_id,
        revision=0,
    )


@pytest.mark.parametrize("executable", [2], indirect=True)
def test_unit_leaves_material_window_once_its_materials_settle(executable):
    db, _, ids = executable
    with Session(db) as session, session.begin():
        steps = [session.get(ExecutionStep, i) for i in ids["MATERIAL"]]
        first_unit = sorted({step.unit_id for step in steps}, key=str)[0]
        assert {row.unit_id for row in session.exec(window_units()).all()} == {
            first_unit
        }
        for step in steps:
            if step.unit_id == first_unit:
                step.status, step.phase = "SUCCEEDED", "DONE"
    with Session(db) as session:
        # 第一个组合的广告仍未创建，但素材已落定：窗口交给下一个组合准备素材。
        admitted = {row.unit_id for row in session.exec(window_units()).all()}
        assert first_unit not in admitted and len(admitted) == 1


def test_ready_ad_compiles_without_locked_per_material_preparation(
    executable, redis_client, monkeypatch
):
    _settle_structure(executable)
    _forbid_locked_preparation(monkeypatch)
    writes = _ad_wire(monkeypatch)
    assert _process(executable, redis_client, executable[2]["AD"][0]) == "SUCCEEDED"
    assert len(writes) == 1
    assert {
        row["creative_info"]["video_info"]["video_id"]: row["creative_info"][
            "image_info"
        ][0]["web_uri"]
        for row in writes[0]["creative_list"]
    } == {"target-0": "cover-0", "target-1": "cover-1"}


def test_ad_waits_for_in_progress_cover_without_relocking(
    executable, redis_client, monkeypatch
):
    ad_id, _, job_id, _ = ready_ad_with_cover_job(
        executable, redis_client, cover_status="PENDING"
    )
    _forbid_locked_preparation(monkeypatch)
    writes = _ad_wire(monkeypatch)
    assert _process(executable, redis_client, ad_id) == "PENDING"
    db = executable[0]
    with Session(db) as session:
        ad = session.get(ExecutionStep, ad_id)
        assert ad.error_code == "material_refresh_required" and ad.request_body is None
        assert [job.id for job in session.exec(select(MaterialCoverJob))] == [job_id]
    assert not writes


def test_repeated_cover_wait_backs_off_but_ready_ad_still_creates_once(
    executable, redis_client, monkeypatch
):
    from app.modules.materials.models import AccountMaterial

    ad_id, _, job_id, _ = ready_ad_with_cover_job(
        executable, redis_client, cover_status="PENDING"
    )
    writes = _ad_wire(monkeypatch)
    db = executable[0]
    for expected_delay in (15, 30, 60, 60):
        with Session(db) as session, session.begin():
            session.get(ExecutionStep, ad_id).due_at = datetime.now(UTC) - timedelta(
                seconds=1
            )
        assert _process(executable, redis_client, ad_id) == "PENDING"
        with Session(db) as session:
            ad = session.get(ExecutionStep, ad_id)
            assert (
                expected_delay - 2
                <= (ad.due_at - datetime.now(UTC)).total_seconds()
                <= expected_delay
            )
            assert ad.request_body is None
    assert writes == []
    with Session(db) as session, session.begin():
        job = session.get(MaterialCoverJob, job_id)
        job.status, job.known_image_id = "READY", "ready-cover"
        session.get(AccountMaterial, job.asset_id).image_id = "ready-cover"
        session.get(ExecutionStep, ad_id).due_at = datetime.now(UTC) - timedelta(
            seconds=1
        )
    assert _process(executable, redis_client, ad_id) == "SUCCEEDED"
    assert len(writes) == 1


@pytest.mark.parametrize("stale", [False, True])
def test_transient_cover_failure_makes_ad_wait_until_recovery_window_ends(
    executable, redis_client, monkeypatch, stale
):
    ad_id, _, job_id, _ = ready_ad_with_cover_job(
        executable, redis_client, cover_status="BLOCKED"
    )
    db = executable[0]
    with Session(db) as session, session.begin():
        job = session.get(MaterialCoverJob, job_id)
        job.request_armed_at = None
        job.error_code = "tiktok_call_deadline_exceeded"
        if stale:
            job.created_at = datetime.now(UTC) - timedelta(hours=3)
    writes = _ad_wire(monkeypatch)
    # 本地锁等待导致的封面失败不是广告失败；超出自动接续时长才按原规则落定。
    assert _process(executable, redis_client, ad_id) == (
        "FAILED" if stale else "PENDING"
    )
    assert not writes


def test_stalled_covers_resume_automatically_without_resending(
    executable, redis_client
):
    db = executable[0]
    _, _, unknown_id, _ = ready_ad_with_cover_job(
        executable, redis_client, cover_status="UNKNOWN"
    )
    with Session(db) as session, session.begin():
        armed = session.get(MaterialCoverJob, unknown_id)
        armed.error_code = "tiktok_local_resources_unavailable"
        armed.repair_after = datetime.now(UTC) - timedelta(seconds=1)
    with Session(db) as session, session.begin():
        assert covers.recover_stalled_covers(session) == 1
    with Session(db) as session:
        job = session.get(MaterialCoverJob, unknown_id)
        # 已 armed 的结果只能只读核查，不能重新上传。
        assert job.status == "VERIFYING"
        assert (
            session.get(PendingDispatch, job.dispatch_id).task_name
            == "materials.verify_cover"
        )


@pytest.mark.parametrize(
    ("code", "age", "resumed"),
    [
        ("tiktok_call_deadline_exceeded", timedelta(0), True),
        ("tiktok_local_resources_unavailable", timedelta(0), True),
        ("cover_source_unavailable", timedelta(0), False),
        ("tiktok_call_deadline_exceeded", timedelta(hours=3), False),
    ],
)
def test_unsent_transient_blocked_cover_is_requeued(
    executable, redis_client, code, age, resumed
):
    db = executable[0]
    _, job_id, _ = pending(executable, redis_client)
    with Session(db) as session, session.begin():
        job = session.get(MaterialCoverJob, job_id)
        job.status, job.error_code, job.dispatch_id = "BLOCKED", code, None
        job.created_at = datetime.now(UTC) - age
        job.repair_after = datetime.now(UTC) - timedelta(seconds=1)
    with Session(db) as session, session.begin():
        covers.recover_stalled_covers(session)
    with Session(db) as session:
        job = session.get(MaterialCoverJob, job_id)
        if resumed:
            assert job.status == "PENDING" and job.error_code is None
            assert (
                session.get(PendingDispatch, job.dispatch_id).task_name
                == "materials.prepare_cover"
            )
        else:
            assert job.status == "BLOCKED" and job.error_code == code
            assert job.dispatch_id is None


@pytest.mark.parametrize(
    "code", ["tiktok_call_deadline_exceeded", "tiktok_local_resources_unavailable"]
)
def test_local_contention_while_claiming_shared_cover_is_retried(
    executable, redis_client, monkeypatch, code
):
    db, context, _ = executable
    _, job_id, _ = pending(executable, redis_client)

    def contended(*_args, **_kwargs):
        raise DomainError(
            code, "本地资源期限耗尽", retryable=code != "tiktok_call_deadline_exceeded"
        )

    monkeypatch.setattr(cover_sharing, "_prepare", contended)
    with Session(db) as session:
        job = session.get(MaterialCoverJob, job_id)
        dispatch_id, revision = job.dispatch_id, job.revision
    covers.run_cover(
        database_engine=db,
        redis_client=redis_client,
        context=context,
        job_id=job_id,
        dispatch_id=dispatch_id,
        revision=revision,
        read=False,
    )
    with Session(db) as session:
        job = session.get(MaterialCoverJob, job_id)
        # 发送前的锁等待/期限耗尽按退避重排，不能落成需要人工处理的 BLOCKED。
        assert job.status == "PENDING" and job.dispatch_id is not None
        assert job.request_armed_at is None


def test_material_settlement_starts_cover_before_ad(executable):
    from app.modules.builds.material_execution import recover_material_results
    from tests.modules.builds.test_cover_execution import remove_cover
    from tests.modules.builds.test_material_execution import unresolved

    db, context, _ = executable
    identity = remove_cover(executable)
    unresolved(db, context, identity, status="ready")
    assert recover_material_results(database_engine=db) == 1
    with Session(db) as session:
        step = session.get(ExecutionStep, identity)
        unit = session.get(BuildUnit, step.unit_id)
        job = session.exec(select(MaterialCoverJob)).one()
        assert (job.material_id, job.advertiser_id) == (
            step.material_id,
            unit.advertiser_id,
        )
        assert job.status == "PENDING" and job.dispatch_id is not None
