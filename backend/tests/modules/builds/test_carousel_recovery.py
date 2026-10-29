"""真实 PG/Redis + 官方 SDK 传输替身；原曲目、租约、迟到回执与 UNKNOWN。"""

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from sqlmodel import Session, select
from urllib3.exceptions import ReadTimeoutError

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.builds import CreatedObject
from app.integrations.tiktok.contracts.common import CallEvidence
from app.integrations.tiktok.gateway import open_tiktok_gateway
from app.modules.builds.carousel_music import _budget
from app.modules.builds.carousel_readiness import (
    body_assets,
    music_proof,
    verify_carousel_ad,
)
from app.modules.builds.execution_models import ExecutionStep, StepEvidence
from app.modules.builds.execution_state import (
    arm_request,
    expire_attempt,
    record_created,
    record_unknown,
)
from tests.integrations.tiktok.test_music_adapters import envelope, music_row
from tests.integrations.tiktok.test_music_adapters import music_wire as music_wire
from tests.modules.builds.test_carousel_execution import carousel_case as carousel_case
from tests.modules.builds.test_carousel_execution import claim_ad, prepare_ad


@pytest.mark.parametrize("source_ingest", [False, True])
def test_submission_retry_only_requeues_proven_unsent_target(
    carousel_case, source_ingest
):
    from app.modules.builds import recovery
    from app.modules.builds.recovery_models import SubmissionRecovery
    from app.modules.materials.models import (
        AccountMaterial,
        MaterialAssetOperation,
        MaterialDistribution,
    )

    engine, context, route, submission, units, _ = carousel_case
    with Session(engine) as session, session.begin():
        step = session.exec(
            select(ExecutionStep).where(
                ExecutionStep.unit_id == units[0], ExecutionStep.kind == "MATERIAL"
            )
        ).first()
        identity = {
            "tenant_id": context.tenant_id,
            "bc_id": route.bc_id,
            "material_id": step.material_id,
            "advertiser_id": "account-A",
        }
        mapping = session.exec(
            select(AccountMaterial).where(
                AccountMaterial.tenant_id == context.tenant_id,
                AccountMaterial.material_id == step.material_id,
                AccountMaterial.advertiser_id == "account-A",
            )
        ).one()
        mapping.status, mapping.is_carousel_usable = "unavailable", False
        op = MaterialAssetOperation(
            **identity,
            frozen_route=route.model_dump(mode="json"),
            path="upload_original",
            status="failed",
            request_digest="a" * 64,
            remote_response={"send_armed": False, "source_ingest": source_ingest},
        )
        session.add(op)
        session.flush()
        dist = MaterialDistribution(
            **identity,
            actor_id=context.actor_id,
            operation_id=op.id,
            target_route=route.model_dump(mode="json"),
            path="upload_original",
            status="blocked",
        )
        session.add(dist)
        session.flush()
        step.status, step.phase, step.error_code = (
            "FAILED",
            "DONE",
            "image_target_unavailable",
        )
        step.distribution_id = dist.id
        step.resolved = {}
        session.flush()
        if source_ingest:
            with pytest.raises(DomainError) as error:
                recovery.request_recovery(
                    session,
                    context=context,
                    submission_id=submission,
                    request_id=uuid4(),
                    kind="RETRY",
                )
            assert error.value.code == "recovery_no_candidates"
            return
        receipt = recovery.request_recovery(
            session,
            context=context,
            submission_id=submission,
            request_id=uuid4(),
            kind="RETRY",
        )
        job = session.get(SubmissionRecovery, receipt.recovery_id)
        payload = {"recovery_id": str(job.id), "revision": job.dispatch_revision}
        step_id, op_id, dist_id = step.id, op.id, dist.id
    recovery.process_recovery(database_engine=engine, context=context, payload=payload)
    recovery.process_recovery(database_engine=engine, context=context, payload=payload)
    with Session(engine) as session:
        job = session.get(SubmissionRecovery, receipt.recovery_id)
        assert job.state == "COMPLETED" and job.scheduled_count == 1
        step = session.get(ExecutionStep, step_id)
        op = session.get(MaterialAssetOperation, op_id)
        assert step.status == "QUEUED" and step.distribution_id == dist_id
        assert op.status == "pending" and op.remote_response["revision"] == 1


def check_music(case, claim, body, redis):
    engine, context, route, *_ = case
    deadline = datetime.now(UTC) + timedelta(seconds=40)
    with open_tiktok_gateway(
        database_engine=engine,
        redis_client=redis,
        context=context,
        route=route,
        task_deadline=deadline,
    ) as gateway:
        return verify_carousel_ad(
            route,
            claim.advertiser_id,
            body_assets(body),
            claim.attempt_id,
            gateway=gateway,
            budget=_budget(deadline, route, "music.get"),
        )


def arm(case, claim, body):
    engine, context, *_ = case
    with Session(engine) as session, session.begin():
        step = session.get(ExecutionStep, claim.step_id)
        step.resolved = {"carousel_music": music_proof(claim, body_assets(body))}
        session.flush()
        arm_request(session, context=context, claim=claim, body=body)


def test_exact_song_new_attempt_empty_and_timeout_are_different(
    carousel_case, music_wire, redis_client
):
    replies, calls = music_wire
    claim = claim_ad(carousel_case)
    _, body = prepare_ad(carousel_case, claim)
    replies.append(envelope([]))
    missing = check_music(carousel_case, claim, body, redis_client)
    assert not missing.ready and missing.issues[0].code == "carousel_music_unavailable"
    replies.append(ReadTimeoutError(None, "/music", "synthetic"))
    with pytest.raises(DomainError) as error:
        check_music(carousel_case, claim, body, redis_client)
    assert error.value.code == "carousel_music_read_failed" and error.value.retryable
    replies.append(envelope([music_row("song", duration=0)]))
    ready = check_music(carousel_case, claim, body, redis_client)
    assert ready.ready and ready.attempt_id == claim.attempt_id
    assert len(calls) == 3
    for _, url, _ in calls:
        query = parse_qs(urlsplit(url).query)
        assert query["search_type"] == ["SEARCH_BY_MUSIC_ID"]
        assert json.loads(query["filtering"][0]) == {"music_ids": ["song"]}
    # 新尝试不能复用上次曲目证据；同曲重新读取。
    new_claim = claim.model_copy(update={"attempt_id": uuid4()})
    replies.append(envelope([music_row("song")]))
    assert (
        check_music(carousel_case, new_claim, body, redis_client).attempt_id
        == new_claim.attempt_id
    )
    assert len(calls) == 4


def test_music_io_has_no_business_transaction_and_binding_change_fences_arm(
    carousel_case, redis_client, monkeypatch
):
    from io import BytesIO

    from urllib3.response import HTTPResponse

    from app.modules.accounts.connection_models import BCConnectionBinding

    engine, context, route, *_ = carousel_case
    claim = claim_ad(carousel_case)
    _, body = prepare_ad(carousel_case, claim)

    def wire(_pool, _method, _url, **_kwargs):
        with Session(engine) as session, session.begin():
            step = session.exec(
                select(ExecutionStep)
                .where(ExecutionStep.id == claim.step_id)
                .with_for_update(nowait=True)
            ).one()
            assert step.phase == "CLAIMED" and step.request_body is None
            binding = session.exec(
                select(BCConnectionBinding).where(
                    BCConnectionBinding.tenant_id == context.tenant_id,
                    BCConnectionBinding.connection_id == route.connection_id,
                )
            ).one()
            binding.revision += 1
        return HTTPResponse(
            body=BytesIO(json.dumps(envelope([music_row("song")])).encode()),
            status=200,
            preload_content=False,
        )

    monkeypatch.setattr("urllib3.connectionpool.HTTPSConnectionPool.urlopen", wire)
    # Gateway may reject the changed authorization after read; otherwise arm must.
    with pytest.raises(DomainError):
        check_music(carousel_case, claim, body, redis_client)
        arm(carousel_case, claim, body)
    with Session(engine) as session:
        assert session.get(ExecutionStep, claim.step_id).request_body is None


def test_armed_unknown_replay_never_reads_music_or_posts(
    carousel_case, redis_client, monkeypatch
):
    from app.modules.builds import execution

    engine, context, *_ = carousel_case
    claim = claim_ad(carousel_case)
    _, body = prepare_ad(carousel_case, claim)
    arm(carousel_case, claim, body)
    with Session(engine) as session, session.begin():
        record_unknown(session, claim=claim, code="synthetic_lost_response")
    monkeypatch.setattr(execution, "_require_bounded_worker", lambda: None)

    def forbidden(*_args, **_kwargs):
        pytest.fail("UNKNOWN replay must not make a fresh request")

    monkeypatch.setattr("urllib3.PoolManager.request", forbidden)
    for _ in range(2):
        assert (
            execution.process_step(
                database_engine=engine,
                redis_client=redis_client,
                context=context,
                step_id=claim.step_id,
                revision=0,
            )
            == "UNKNOWN"
        )
    with Session(engine) as session:
        step = session.get(ExecutionStep, claim.step_id)
        assert step.request_body == body and step.attempt_id == claim.attempt_id


def test_late_created_after_lease_expiry_preserves_original_body(carousel_case):
    engine, _, *_ = carousel_case
    claim = claim_ad(carousel_case)
    _, body = prepare_ad(carousel_case, claim)
    arm(carousel_case, claim, body)
    with Session(engine) as session, session.begin():
        step = session.get(ExecutionStep, claim.step_id)
        step.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        assert expire_attempt(session, step=step) == "RECONCILE"
        result = CreatedObject(
            kind="AD",
            remote_id="late-ad",
            operation_status="ENABLE",
            evidence=CallEvidence(),
        )
        assert record_created(session, claim=claim, result=result) == "UNKNOWN"
        assert step.request_body == body
        assert (
            session.exec(
                select(StepEvidence).where(
                    StepEvidence.step_id == step.id,
                    StepEvidence.conclusion == "LATE_CREATED",
                )
            )
            .one()
            .summary["remote_id"]
            == "late-ad"
        )


def test_official_sdk_create_and_physical_fence_preserve_complete_ad(
    carousel_case, redis_client, monkeypatch
):
    from io import BytesIO

    from urllib3.response import HTTPResponse

    from app.modules.builds.cover_execution import validate_ad_assets
    from app.modules.builds.preview_models import BuildUnit

    engine, context, route, *_ = carousel_case
    claim = claim_ad(carousel_case)
    intent, body = prepare_ad(carousel_case, claim)
    calls = []

    def transport(_pool, method, url, **kwargs):
        calls.append((method, url))
        with Session(engine) as session:
            step = session.exec(
                select(ExecutionStep)
                .where(ExecutionStep.id == claim.step_id)
                .with_for_update(nowait=True)
            ).one()
            if "/music/" in url:
                assert step.phase == "CLAIMED" and step.request_body is None
                payload = envelope([music_row("song", duration=0)])
            else:
                actual = json.loads(kwargs["body"])
                assert step.phase == "REQUEST_ARMED" and actual == body
                assert "video_info" not in actual["creative_list"][0]["creative_info"]
                assert (
                    len(actual["creative_list"][0]["creative_info"]["image_info"]) == 3
                )
                payload = {
                    "code": 0,
                    "data": {
                        "smart_plus_ad_id": "synthetic-ad",
                        "operation_status": "ENABLE",
                    },
                    "request_id": "offline-create",
                }
        return HTTPResponse(
            body=BytesIO(json.dumps(payload).encode()),
            status=200,
            preload_content=False,
        )

    monkeypatch.setattr("urllib3.connectionpool.HTTPSConnectionPool.urlopen", transport)
    armed = False

    def before():
        if armed:
            with Session(engine) as session:
                step = session.get(ExecutionStep, claim.step_id)
                validate_ad_assets(
                    session,
                    step=step,
                    unit=session.get(BuildUnit, claim.unit_id),
                    body=body,
                )

    deadline = datetime.now(UTC) + timedelta(seconds=40)
    with open_tiktok_gateway(
        database_engine=engine,
        redis_client=redis_client,
        context=context,
        route=route,
        task_deadline=deadline,
        before_request=before,
    ) as gateway:
        assert verify_carousel_ad(
            route,
            claim.advertiser_id,
            body_assets(body),
            claim.attempt_id,
            gateway=gateway,
            budget=_budget(deadline, route, "music.get"),
        ).ready
        arm(carousel_case, claim, body)
        armed = True
        result = gateway.builds.create(attempt_id=claim.attempt_id, intent=intent)
    with Session(engine) as session, session.begin():
        assert record_created(session, claim=claim, result=result) == "SUCCEEDED"
    assert [method for method, _ in calls] == ["GET", "POST"]


def test_physical_fence_rejects_late_image_change_without_substitution(carousel_case):
    from app.modules.builds.cover_execution import validate_ad_assets
    from app.modules.builds.preview_models import BuildUnit
    from app.modules.materials.models import AccountMaterial

    engine, context, *_ = carousel_case
    claim = claim_ad(carousel_case)
    _, body = prepare_ad(carousel_case, claim)
    arm(carousel_case, claim, body)
    with Session(engine) as session, session.begin():
        asset = session.exec(
            select(AccountMaterial).where(
                AccountMaterial.tenant_id == context.tenant_id,
                AccountMaterial.image_id == body_assets(body)[0].image_ids[0],
            )
        ).one()
        asset.status = "unavailable"
        asset.is_carousel_usable = False
        session.flush()
        step = session.get(ExecutionStep, claim.step_id)
        with pytest.raises(DomainError):
            validate_ad_assets(
                session,
                step=step,
                unit=session.get(BuildUnit, claim.unit_id),
                body=body,
            )
        assert step.request_body == body


def test_unknown_readback_uses_original_complete_body_and_only_get(
    carousel_case, redis_client, monkeypatch
):
    from urllib3.response import HTTPResponse

    from app.modules.builds import reconciliation

    engine, context, *_ = carousel_case
    claim = claim_ad(carousel_case)
    _, body = prepare_ad(carousel_case, claim)
    arm(carousel_case, claim, body)
    with Session(engine) as session, session.begin():
        record_unknown(session, claim=claim, code="synthetic_response_lost")
        session.get(ExecutionStep, claim.step_id).lease_expires_at = datetime.now(
            UTC
        ) - timedelta(seconds=1)
    calls = []

    def transport(_pool, method, url, **_kwargs):
        calls.append((method, url))
        assert method == "GET" and "/music/" not in url
        return HTTPResponse(
            body=json.dumps(
                {
                    "code": 0,
                    "request_id": "synthetic-readback",
                    "data": {
                        "list": [{**body, "smart_plus_ad_id": "only-original"}],
                        "page_info": {
                            "page": 1,
                            "page_size": 100,
                            "total_number": 1,
                            "total_page": 1,
                        },
                    },
                }
            ).encode(),
            status=200,
        )

    monkeypatch.setattr("urllib3.PoolManager.request", transport)
    monkeypatch.setattr(reconciliation, "require_bounded_worker", lambda: None)
    for _ in range(2):
        result = reconciliation.process_reconciliation(
            database_engine=engine,
            redis_client=redis_client,
            context=context,
            step_id=claim.step_id,
            revision=0,
        )
        assert result.state == "SUCCEEDED"
    assert len(calls) == 1
    with Session(engine) as session:
        step = session.get(ExecutionStep, claim.step_id)
        assert step.remote_id == "only-original" and step.request_body == body
