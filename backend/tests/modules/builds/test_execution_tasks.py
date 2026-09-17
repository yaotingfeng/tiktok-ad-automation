"""Durable outbox -> official SDK -> readback, entirely offline remote transport."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import pytest
from sqlmodel import Session, col, select
from urllib3.response import HTTPResponse

from app.jobs.models import PendingDispatch
from app.modules.builds import reconciliation
from app.modules.builds.dispatch import (
    READ_TASK,
    STEP_TASK,
    UNIT_TASK,
    process_unit,
    repair_execution,
)
from app.modules.builds.execution_models import (
    ExecutionStep,
    Submission,
    SubmissionUnit,
)
from app.modules.builds.tasks import deliver_step, finish_delivery
from tests.modules.builds.test_execution import executable as executable


@pytest.mark.parametrize(
    "executable, lose_campaign_receipt, remote_match",
    [
        (1, False, True),
        (1, True, True),
        (1, True, False),
        (2, False, True),
        (12, False, True),
    ],
    indirect=["executable"],
)
def test_outbox_builds_and_reads_all_layers_without_repeating_any_create(
    executable, redis_client, monkeypatch, lose_campaign_receipt, remote_match
):
    db, context, _ = executable
    with Session(db) as session:
        unit_count = len(session.exec(select(SubmissionUnit)).all())
    if unit_count > 2:
        from app.modules.builds import execution_window

        monkeypatch.setattr(execution_window, "MAX_ACTIVE_UNITS", 10)
    monkeypatch.setattr(reconciliation, "require_bounded_worker", lambda: None)
    remote, calls = {}, []

    def transport(_pool, method, url, **kwargs):
        path = urlsplit(url).path
        kind = "cta" if "portfolio" in path else path.split("/")[-3]
        key = {
            "cta": "creative_portfolio_id",
            "campaign": "campaign_id",
            "adgroup": "adgroup_id",
            "ad": "smart_plus_ad_id",
        }[kind]
        calls.append((method, path))
        if method == "POST":
            body = json.loads(kwargs["body"])
            identity = f"actual-{kind}-{len(remote)}"
            remote[identity] = {**body, key: identity}
            # 合成服务回读用精确十进制字符串；不把 SDK float 当远端精度证明。
            for amount in ("budget", "roas_bid"):
                if amount in remote[identity]:
                    remote[identity][amount] = str(remote[identity][amount])
            if lose_campaign_receipt and kind == "campaign":
                raise OSError("remote created before connection loss")
            data = {key: identity, "operation_status": "ENABLE"}
        else:
            assert method == "GET"
            query = dict(kwargs["fields"])
            if kind == "cta":
                data = deepcopy(remote[query["creative_portfolio_id"]])
            else:
                filters = json.loads(query["filtering"])
                rows = [
                    deepcopy(r)
                    for r in remote.values()
                    if key in r
                    and all(
                        r.get(k[:-1] if k.endswith("_ids") else k) in v
                        if isinstance(v, list)
                        else r.get(k) == v
                        for k, v in filters.items()
                    )
                ]
                if not remote_match and kind == "campaign":
                    rows = []
                data = {
                    "list": rows,
                    "page_info": {
                        "page": 1,
                        "page_size": 100,
                        "total_number": len(rows),
                        "total_page": 1 if rows else 0,
                    },
                }
        return HTTPResponse(
            body=json.dumps(
                {"code": 0, "request_id": "offline-wire", "data": data}
            ).encode(),
            status=200,
        )

    monkeypatch.setattr("urllib3.PoolManager.request", transport)
    for _ in range(max(150, unit_count * 40)):
        with Session(db) as session, session.begin():
            message = session.exec(
                select(PendingDispatch)
                .where(
                    PendingDispatch.tenant_id == context.tenant_id,
                    col(PendingDispatch.task_name).in_(
                        [UNIT_TASK, STEP_TASK, READ_TASK]
                    ),
                    col(PendingDispatch.published_at).is_(None),
                    PendingDispatch.available_at <= datetime.now(UTC),
                )
                .order_by(col(PendingDispatch.available_at), col(PendingDispatch.id))
                .limit(1)
            ).first()
            if message is not None:
                name, payload = message.task_name, message.payload
                message.published_at = datetime.now(UTC)
                session.add(message)
        if message is None:
            # Simulate elapsed recovery time after the SDK has returned. The
            # source lease intentionally survives an ambiguous create receipt.
            with Session(db) as session, session.begin():
                uncertain = session.exec(
                    select(ExecutionStep).where(
                        ExecutionStep.status == "UNKNOWN",
                        col(ExecutionStep.dispatch_id).is_not(None),
                        col(ExecutionStep.lease_expires_at).is_not(None),
                    )
                ).all()
                for step in uncertain:
                    step.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
                    step.updated_at = datetime.now(UTC) - timedelta(seconds=200)
                    session.add(step)
            if uncertain:
                repair_execution(database_engine=db)
                continue
            if repair_execution(database_engine=db):
                continue
            if unit_count > 2:
                from time import sleep

                with Session(db) as session:
                    next_due = session.exec(
                        select(PendingDispatch.available_at)
                        .where(
                            col(PendingDispatch.task_name).in_(
                                [UNIT_TASK, STEP_TASK, READ_TASK]
                            ),
                            col(PendingDispatch.published_at).is_(None),
                            PendingDispatch.available_at > datetime.now(UTC),
                        )
                        .order_by(PendingDispatch.available_at)
                        .limit(1)
                    ).first()
                if next_due is not None:
                    # 大于测试额度窗口的调用必须等待真实Redis退避，不能把
                    # 尚未到期误当成图已收敛，也不删除限流键来伪造吞吐。
                    sleep(
                        min(
                            15,
                            max(0.05, (next_due - datetime.now(UTC)).total_seconds()),
                        )
                    )
                    continue
            break
        if name == UNIT_TASK:
            process_unit(database_engine=db, context=context, payload=payload)
        else:
            deliver_step(
                database_engine=db,
                redis_client=redis_client,
                context=context,
                payload=payload,
                reconcile=name == READ_TASK,
            )
    else:
        raise AssertionError("bounded outbox did not settle")
    with Session(db) as session:
        steps = session.exec(select(ExecutionStep)).all()
        if not remote_match:
            campaign = next(s for s in steps if s.kind == "CAMPAIGN")
            assert campaign.status == "UNKNOWN" and campaign.dispatch_id is None
            assert campaign.resolved["dispatch_reconciliation_done"] is True
            assert session.exec(select(Submission)).one().status == "NEEDS_REVIEW"
            assert sum(m == "POST" for m, _ in calls) == 2
            assert sum(m == "GET" for m, _ in calls) == 1
            return
        assert all(s.status == "SUCCEEDED" for s in steps), [
            (s.kind, s.status, s.error_code) for s in steps
        ]
        assert all(not s.mismatch for s in steps)
        assert session.exec(select(Submission)).one().status == "COMPLETED"
        assert all(
            unit.dispatch_id is None
            for unit in session.exec(select(SubmissionUnit)).all()
        )
    assert sum(m == "POST" for m, _ in calls) == 5 * unit_count
    assert sum(m == "GET" for m, _ in calls) >= 4 * unit_count


def test_successful_receipt_lost_before_continuation_is_repaired(executable):
    from app.modules.builds.dispatch import repair_execution

    db, context, ids = executable
    with Session(db) as session:
        unit = session.exec(select(SubmissionUnit)).one()
        payload = {"unit_id": str(unit.unit_id), "revision": unit.dispatch_revision}
    process_unit(database_engine=db, context=context, payload=payload)
    with Session(db) as session, session.begin():
        step = session.get(ExecutionStep, ids["CTA"][0])
        step.status, step.phase, step.remote_id = "SUCCEEDED", "DONE", "known-cta"
        step.updated_at = step.due_at = datetime.now(UTC) - timedelta(seconds=200)
        message = session.get(PendingDispatch, step.dispatch_id)
        message.published_at = step.updated_at
        original = step.dispatch_id, step.dispatch_revision
        session.add_all([step, message])
    assert repair_execution(database_engine=db) >= 1
    with Session(db) as session:
        step = session.get(ExecutionStep, ids["CTA"][0])
        assert (step.dispatch_id, step.dispatch_revision) == original
        assert session.get(PendingDispatch, original[0]).published_at is None
    finish_delivery(
        database_engine=db, context=context, step_id=ids["CTA"][0], revision=original[1]
    )
    with Session(db) as session:
        assert session.get(ExecutionStep, ids["CTA"][0]).dispatch_id is None
        unit = session.exec(select(SubmissionUnit)).one()
        next_dispatch = unit.dispatch_id
        assert next_dispatch
    finish_delivery(
        database_engine=db, context=context, step_id=ids["CTA"][0], revision=original[1]
    )
    with Session(db) as session:
        assert session.exec(select(SubmissionUnit)).one().dispatch_id == next_dispatch


def test_pending_cover_waits_without_repeated_step_or_unit_messages(
    executable, redis_client
):
    from app.modules.builds.dispatch import queue_step
    from app.modules.materials.cover_models import MaterialCoverJob
    from tests.modules.builds.test_cover_execution import pending

    identity, job_id, _ = pending(executable, redis_client)
    db, context, _ = executable
    with Session(db) as session, session.begin():
        step = session.get(ExecutionStep, identity)
        row = session.get(Submission, step.submission_id)
        queue_step(session, step=step, submission=row)
        step.status = "PENDING"
        revision = step.dispatch_revision
    finish_delivery(
        database_engine=db, context=context, step_id=identity, revision=revision
    )
    with Session(db) as session:
        step = session.get(ExecutionStep, identity)
        assert step.status == "PENDING" and step.dispatch_id is None
        unit = session.exec(select(SubmissionUnit)).one()
        waiting_messages = select(PendingDispatch.id).where(
            (col(PendingDispatch.payload)["step_id"].astext == str(identity))
            | (col(PendingDispatch.payload)["unit_id"].astext == str(unit.unit_id))
        )
        before = set(session.exec(waiting_messages).all())
        payload = {"unit_id": str(unit.unit_id), "revision": unit.dispatch_revision}
    process_unit(database_engine=db, context=context, payload=payload)
    for _ in range(3):
        repair_execution(database_engine=db)
    with Session(db) as session:
        step = session.get(ExecutionStep, identity)
        assert step.dispatch_id is None and step.dispatch_revision == revision
        assert set(session.exec(waiting_messages).all()) == before
    with Session(db) as session, session.begin():
        job = session.get(MaterialCoverJob, job_id)
        job.status = "READY"
        job.known_image_id = "verified-cover"
        job.request_armed_at = datetime.now(UTC)
        job.dispatch_id = None
    repair_execution(database_engine=db)
    with Session(db) as session:
        step = session.get(ExecutionStep, identity)
        assert step.status == "QUEUED" and step.dispatch_revision == revision + 1
        dispatch_id = step.dispatch_id
        assert dispatch_id
    repair_execution(database_engine=db)
    with Session(db) as session:
        assert session.get(ExecutionStep, identity).dispatch_id == dispatch_id


@pytest.mark.parametrize("executable", [2], indirect=True)
def test_submission_admits_one_material_unit_and_preserves_other_intent(executable):
    db, context, _ = executable
    with Session(db) as session:
        units = session.exec(
            select(SubmissionUnit).order_by(SubmissionUnit.unit_id)
        ).all()
        payloads = [
            {"unit_id": str(unit.unit_id), "revision": unit.dispatch_revision}
            for unit in units
        ]
    for payload in payloads:
        process_unit(database_engine=db, context=context, payload=payload)
    with Session(db) as session:
        materials = session.exec(
            select(ExecutionStep).where(ExecutionStep.kind == "MATERIAL")
        ).all()
        assert len({s.unit_id for s in materials if s.status == "QUEUED"}) == 1
        waiting = [s for s in materials if s.unit_id == units[1].unit_id]
        assert len(waiting) == 2
        assert all(
            s.status == "PENDING"
            and s.error_code == "execution_window_wait"
            and s.dispatch_id is None
            for s in waiting
        )


@pytest.mark.parametrize("executable", [2], indirect=True)
def test_old_queued_future_material_parks_and_unknown_unit_yields_window(
    executable, redis_client
):
    from app.modules.builds.dispatch import queue_step

    db, context, _ = executable
    with Session(db) as session, session.begin():
        units = session.exec(
            select(SubmissionUnit).order_by(SubmissionUnit.unit_id)
        ).all()
        first_unit_id = units[0].unit_id
        future = session.exec(
            select(ExecutionStep).where(
                ExecutionStep.unit_id == units[1].unit_id,
                ExecutionStep.kind == "MATERIAL",
            )
        ).first()
        row = session.get(Submission, future.submission_id)
        queue_step(session, step=future, submission=row)
        identity, revision = future.id, future.dispatch_revision
    deliver_step(
        database_engine=db,
        redis_client=redis_client,
        context=context,
        payload={"step_id": str(identity), "revision": revision},
    )
    with Session(db) as session, session.begin():
        parked = session.get(ExecutionStep, identity)
        assert parked.status == "PENDING" and parked.dispatch_id is None
        assert parked.error_code == "execution_window_wait"
        first = session.exec(
            select(ExecutionStep).where(
                ExecutionStep.unit_id == first_unit_id,
                ExecutionStep.kind == "MATERIAL",
            )
        ).first()
        first.status, first.phase, first.error_code = (
            "UNKNOWN",
            "DONE",
            "material_result_unknown",
        )
        first_id = first.id
    assert repair_execution(database_engine=db) >= 1
    with Session(db) as session:
        assert session.get(ExecutionStep, identity).status == "QUEUED"
        assert session.get(ExecutionStep, first_id).status == "UNKNOWN"


def test_cover_result_after_video_expiry_runs_refresh_before_success(
    executable, redis_client
):
    from app.modules.materials.cover_models import MaterialCoverJob
    from app.modules.materials.models import AccountMaterial
    from tests.modules.builds.test_cover_execution import pending

    identity, job_id, _ = pending(executable, redis_client)
    db, context, _ = executable
    with Session(db) as session, session.begin():
        job = session.get(MaterialCoverJob, job_id)
        job.status, job.known_image_id, job.dispatch_id = (
            "READY",
            "verified-cover",
            None,
        )
        job.request_armed_at = job.updated_at = datetime.now(UTC)
        asset = session.get(AccountMaterial, job.asset_id)
        asset.image_id = job.known_image_id
        asset.verified_at = datetime.now(UTC) - timedelta(hours=1)
    repair_execution(database_engine=db)
    with Session(db) as session:
        step = session.get(ExecutionStep, identity)
        payload = {"step_id": str(identity), "revision": step.dispatch_revision}
    deliver_step(
        database_engine=db, redis_client=redis_client, context=context, payload=payload
    )
    with Session(db) as session:
        step = session.get(ExecutionStep, identity)
        assert step.status == "PENDING" and step.error_code == "material_pending"
        assert step.distribution_id and step.dispatch_id is None


@pytest.mark.parametrize("executable", [2], indirect=True)
def test_future_unarmed_cover_waits_for_its_build_window(executable, redis_client):
    from app.modules.builds.preview_models import BuildUnit
    from app.modules.builds.routes import load_preview_route
    from app.modules.materials import covers
    from app.modules.materials.cover_models import MaterialCoverJob
    from app.modules.materials.models import AccountMaterial, MaterialFile

    db, context, _ = executable
    with Session(db) as session, session.begin():
        units = session.exec(
            select(SubmissionUnit).order_by(SubmissionUnit.unit_id)
        ).all()
        first_unit_id = units[0].unit_id
        step = session.exec(
            select(ExecutionStep).where(
                ExecutionStep.unit_id == units[1].unit_id,
                ExecutionStep.kind == "MATERIAL",
            )
        ).first()
        frozen = session.get(BuildUnit, step.unit_id)
        session.get(MaterialFile, step.material_id).video_md5 = "a" * 32
        asset = session.exec(
            select(AccountMaterial).where(
                AccountMaterial.material_id == step.material_id,
                AccountMaterial.advertiser_id == frozen.advertiser_id,
            )
        ).one()
        asset.image_id = None
        prepared = covers.ensure_cover(
            session,
            context=context,
            bc_id=step.bc_id,
            material_id=step.material_id,
            advertiser_id=frozen.advertiser_id,
            task_key="future-window-cover",
            route=load_preview_route(
                session, context=context, preview_id=step.preview_id
            ),
        )
        step.cover_job_id = prepared.task_id
        job = session.get(MaterialCoverJob, prepared.task_id)
        identity, dispatch_id, revision = job.id, job.dispatch_id, job.revision
    covers.run_cover(
        database_engine=db,
        redis_client=redis_client,
        context=context,
        job_id=identity,
        dispatch_id=dispatch_id,
        revision=revision,
        read=False,
    )
    with Session(db) as session, session.begin():
        job = session.get(MaterialCoverJob, identity)
        assert job.status == "PENDING" and job.error_code == "cover_window_wait"
        assert job.dispatch_id is None and job.request_armed_at is None
        job.repair_after = datetime.now(UTC) - timedelta(seconds=1)
        covers.repair_cover_dispatches(session)
        assert job.dispatch_id is None
        first = session.exec(
            select(ExecutionStep).where(
                ExecutionStep.unit_id == first_unit_id, ExecutionStep.kind == "MATERIAL"
            )
        ).first()
        first.status, first.phase = "UNKNOWN", "DONE"
        covers.repair_cover_dispatches(session)
        assert job.dispatch_id is not None and job.request_armed_at is None


@pytest.mark.parametrize("executable", [3], indirect=True)
def test_cover_planner_does_not_pull_future_queued_jobs_into_current_batch(executable):
    from app.modules.builds.preview_models import BuildUnit
    from app.modules.builds.routes import load_preview_route
    from app.modules.materials import cover_sharing, covers
    from app.modules.materials.cover_models import MaterialCoverJob
    from app.modules.materials.models import AccountMaterial, MaterialFile

    database, context, _ = executable
    with Session(database) as db, db.begin():
        units = db.exec(select(SubmissionUnit).order_by(SubmissionUnit.unit_id)).all()
        first_step = db.exec(
            select(ExecutionStep).where(
                ExecutionStep.unit_id == units[0].unit_id,
                ExecutionStep.kind == "MATERIAL",
            )
        ).first()
        material = db.get(MaterialFile, first_step.material_id)
        material.video_md5 = "a" * 32
        route = load_preview_route(
            db, context=context, preview_id=first_step.preview_id
        )
        jobs = []
        for index, unit in enumerate(units):
            frozen = db.get(BuildUnit, unit.unit_id)
            asset = db.exec(
                select(AccountMaterial).where(
                    AccountMaterial.material_id == material.id,
                    AccountMaterial.advertiser_id == frozen.advertiser_id,
                )
            ).one()
            asset.image_id = None
            method = covers.ensure_source_cover if index == 2 else covers.ensure_cover
            result = method(
                db,
                context=context,
                bc_id=first_step.bc_id,
                material_id=material.id,
                advertiser_id=frozen.advertiser_id,
                task_key=f"window-plan-{index}",
                route=route,
            )
            job = db.get(MaterialCoverJob, result.task_id)
            jobs.append(job)
            if index < 2:
                step = db.exec(
                    select(ExecutionStep).where(
                        ExecutionStep.unit_id == unit.unit_id,
                        ExecutionStep.kind == "MATERIAL",
                        ExecutionStep.material_id == material.id,
                    )
                ).one()
                step.cover_job_id = job.id
            else:
                job.status, job.request_armed_at = "READY", datetime.now(UTC)
                job.known_image_id, job.signature = "tos-source-window", "b" * 32
                job.candidate_image_id = None
                job.width, job.height, job.image_mid = 720, 1280, "900000"
                job.dispatch_id = None
                asset.image_id = job.known_image_id
        first, future, _ = jobs
        claimed = covers._claim_in_session(
            db, context, first.id, first.dispatch_id, first.revision, read=False
        )
        assert claimed is not None
        prepared = cover_sharing._prepare(db, context, *claimed)
        assert prepared is not None
        batch, _ = prepared
        assert {member["job_id"] for member in batch.members} == {str(first.id)}
        db.refresh(future)
        assert future.share_batch_id is None and future.request_armed_at is None


@pytest.mark.parametrize("executable", [12], indirect=True)
def test_parallel_window_is_bounded_and_replaces_blocked_unit(executable, monkeypatch):
    from app.modules.builds import execution_window

    monkeypatch.setattr(execution_window, "MAX_ACTIVE_UNITS", 10, raising=False)
    database, context, _ = executable
    with Session(database) as db, db.begin():
        admitted = db.exec(execution_window.window_units()).all()
        assert len(admitted) == 10
        assert all(
            execution_window.material_unit_admitted(
                db, tenant_id=context.tenant_id, submission_id=row[1], unit_id=row[2]
            )
            for row in admitted
        )
        blocked = admitted[0][2]
        step = db.exec(
            select(ExecutionStep).where(
                ExecutionStep.unit_id == blocked, ExecutionStep.kind == "MATERIAL"
            )
        ).first()
        step.status, step.phase = "UNKNOWN", "DONE"
        after = db.exec(execution_window.window_units()).all()
        assert len(after) == 10 and blocked not in {row[2] for row in after}
        assert len({row[2] for row in after} - {row[2] for row in admitted}) == 1


@pytest.mark.parametrize("executable", [3], indirect=True)
@pytest.mark.parametrize("case", ["future", "mixed", "armed"])
def test_existing_cover_batch_respects_window_without_stranding_members(
    executable, case, monkeypatch
):
    from datetime import timedelta

    from app.modules.builds.preview_models import BuildUnit
    from app.modules.builds.routes import load_preview_route
    from app.modules.materials import covers
    from app.modules.materials.cover_models import (
        MaterialCoverJob,
        MaterialCoverShareBatch,
    )
    from app.modules.materials.models import AccountMaterial, MaterialFile

    database, context, _ = executable
    with Session(database) as db, db.begin():
        units = db.exec(select(SubmissionUnit).order_by(SubmissionUnit.unit_id)).all()
        jobs = []
        for index, unit in enumerate(units):
            step = db.exec(
                select(ExecutionStep).where(
                    ExecutionStep.unit_id == unit.unit_id,
                    ExecutionStep.kind == "MATERIAL",
                )
            ).first()
            material = db.get(MaterialFile, step.material_id)
            material.video_md5 = "a" * 32
            frozen = db.get(BuildUnit, unit.unit_id)
            asset = db.exec(
                select(AccountMaterial).where(
                    AccountMaterial.material_id == material.id,
                    AccountMaterial.advertiser_id == frozen.advertiser_id,
                )
            ).one()
            asset.image_id = None
            route = load_preview_route(db, context=context, preview_id=step.preview_id)
            result = covers.ensure_cover(
                db,
                context=context,
                bc_id=step.bc_id,
                material_id=material.id,
                advertiser_id=frozen.advertiser_id,
                task_key=f"existing-batch-{index}",
                route=route,
            )
            job = db.get(MaterialCoverJob, result.task_id)
            step.cover_job_id = job.id
            jobs.append(job)
        members = [jobs[2], jobs[0] if case == "mixed" else jobs[1]]
        batch = MaterialCoverShareBatch(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            bc_id=jobs[1].bc_id,
            source_advertiser_id=jobs[0].advertiser_id,
            source_route=jobs[0].frozen_route,
            target_route=jobs[1].frozen_route,
            members=[{"job_id": str(job.id)} for job in members],
            wake_job_id=jobs[2].id,
            armed_at=datetime.now(UTC) if case == "armed" else None,
        )
        db.add(batch)
        db.flush()
        for job in members:
            job.share_batch_id = batch.id
            if case == "armed":
                job.request_armed_at = batch.armed_at
        anchor = jobs[2]
        claimed = covers._claim_in_session(
            db, context, anchor.id, anchor.dispatch_id, anchor.revision, read=False
        )
        if case == "mixed":
            assert claimed is not None
        elif case == "armed":
            assert claimed is None and anchor.status == "VERIFYING"
        else:
            assert claimed is None, "未来已分批任务仍占用准备执行槽"
            assert (
                anchor.error_code == "cover_window_wait" and anchor.dispatch_id is None
            )
            # 当前组合完成后，原批次的wake任务必须由正式repair重新接续。
            for step in db.exec(
                select(ExecutionStep).where(ExecutionStep.unit_id == units[0].unit_id)
            ).all():
                step.status = "SUCCEEDED"
            due = anchor.repair_after + timedelta(seconds=1)
            with monkeypatch.context() as patch:
                patch.setattr(covers, "_now", lambda: due)
                assert covers.repair_cover_dispatches(db) >= 1
            assert anchor.dispatch_id is not None and anchor.share_batch_id == batch.id


def test_readback_receipt_lost_before_continuation_keeps_read_delivery(executable):
    from app.modules.builds.dispatch import queue_step

    db, context, ids = executable
    with Session(db) as session, session.begin():
        step = session.get(ExecutionStep, ids["CAMPAIGN"][0])
        row = session.get(Submission, step.submission_id)
        step.status, step.phase = "UNKNOWN", "DONE"
        queue_step(session, step=step, submission=row, reconcile=True)
        original = step.dispatch_id, step.dispatch_revision
        # A readback committed its successful result, then the worker died.
        step.status, step.remote_id = "SUCCEEDED", "read-known-campaign"
        step.updated_at = step.due_at = datetime.now(UTC) - timedelta(seconds=200)
        msg = session.get(PendingDispatch, step.dispatch_id)
        msg.published_at = step.updated_at
        session.add_all([step, msg])
    repair_execution(database_engine=db)
    with Session(db) as session:
        step = session.get(ExecutionStep, ids["CAMPAIGN"][0])
        assert step.status == "SUCCEEDED" and not step.mismatch
        assert (step.dispatch_id, step.dispatch_revision) == original
        msg = session.get(PendingDispatch, step.dispatch_id)
        assert msg.task_name == READ_TASK and msg.published_at is None
