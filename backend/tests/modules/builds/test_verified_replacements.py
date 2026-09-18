"""补建仅在真实 gateway 读证据通过后闭环；原 UNKNOWN 永久保留。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlmodel import Session, select

from app.core.errors import DomainError
from app.modules.builds.execution_models import ExecutionStep
from app.modules.builds.request_compiler import decode_intent, encode_intent
from app.modules.builds.routes import save_attempt_context
from tests.modules.builds.test_reconciliation import arm, wire
from tests.modules.builds.test_reconciliation import recon_env as recon_env


@pytest.fixture
def replacement_env(recon_env):
    env = recon_env
    with Session(env.engine) as session:
        from app.modules.tenants.models import TenantMembership

        member = session.get(
            TenantMembership, (env.context.tenant_id, env.context.actor_id)
        )
        member.role = "tenant_admin"
        session.add(member)
        source = session.get(ExecutionStep, env.ids["AD"])
        env.submission_id = source.submission_id
        env.ids["ADGROUP"] = source.parent_step_id
        for step in session.exec(
            select(ExecutionStep).where(
                ExecutionStep.submission_id == source.submission_id
            )
        ).all():
            step.status, step.phase = "SUCCEEDED", "DONE"
            if step.kind in {"CAMPAIGN", "ADGROUP", "AD", "CTA"}:
                step.remote_id = f"original-{step.id}"
            session.add(step)
        save_attempt_context(session, step=source)
        session.commit()
    _, group_body = arm(env, "ADGROUP", known="group-parent")
    _, ad_body = arm(env, "AD")
    original_source = env.ids["AD"]
    with Session(env.engine) as session:
        sibling = session.exec(
            select(ExecutionStep).where(
                ExecutionStep.parent_step_id == env.ids["ADGROUP"],
                ExecutionStep.kind == "AD",
                ExecutionStep.id != original_source,
            )
        ).one()
        save_attempt_context(session, step=sibling)
        env.sibling_id = sibling.id
        session.commit()
    env.ids["AD"] = env.sibling_id
    arm(env, "AD")
    env.ids["AD"] = original_source
    with Session(env.engine) as session:
        source = session.get(ExecutionStep, env.ids["AD"])
        env.source_digest = source.request_body_digest
        child = session.exec(
            select(ExecutionStep).where(
                ExecutionStep.parent_step_id == source.id,
                ExecutionStep.kind == "READBACK",
            )
        ).one()
        child.status, child.phase, child.error_code = (
            "FAILED",
            "DONE",
            "dependency_failed",
        )
        session.add(child)
        env.readback_id = child.id
        session.commit()
    env.original_ad = decode_intent("AD", ad_body)
    env.original_group = decode_intent("ADGROUP", group_body)
    env.ad = env.original_ad.model_copy(
        update={"adgroup_id": "replacement-group", "name": "replacement ad"}
    )
    env.group = env.original_group.model_copy(
        update={
            "name": "replacement group",
            "schedule_start_time": "2026-09-15 10:00:00",
        }
    )
    env.request_id = uuid4()
    return env


def invoke(env, **changes):
    from app.modules.builds.corrections import import_verified_replacement

    args = {
        "database_engine": env.engine,
        "redis_client": env.redis,
        "context": env.context,
        "request_id": env.request_id,
        "source_step_id": env.ids["AD"],
        "source_request_digest": env.source_digest,
        "replacement_ad_id": "replacement-ad",
        "replacement_adgroup_id": "replacement-group",
        "ad_intent": env.ad,
        "adgroup_intent": env.group,
        "audit_reference": "private-evidence:approved-replacement",
        "task_deadline": datetime.now(UTC) + timedelta(seconds=40),
    }
    return import_verified_replacement(**(args | changes))


def responses(env, *, ad_status="ENABLE", old_status="DISABLE", mismatch=False):
    def rows(path, query):
        if path.endswith("/smart_plus/ad/get/"):
            body = encode_intent(env.ad)
            if mismatch:
                body["ad_text_list"] = [{"ad_text": "wrong copy"}]
            return [
                {
                    **body,
                    "smart_plus_ad_id": "replacement-ad",
                    "operation_status": ad_status,
                }
            ]
        if path.endswith("/smart_plus/adgroup/get/"):
            return [{**encode_intent(env.group), "adgroup_id": "replacement-group"}]
        if path.endswith("/adgroup/get/"):
            identity = query["filtering"]["adgroup_ids"][0]
            return [
                {
                    "advertiser_id": env.ad.advertiser_id,
                    "adgroup_id": identity,
                    "operation_status": old_status
                    if identity == "group-parent"
                    else "ENABLE",
                }
            ]
        raise AssertionError(path)

    return rows


def test_verified_same_group_reissue_keeps_successful_sibling_and_resolves_source(
    replacement_env, monkeypatch
):
    """Removing the same-group branch must make an approved one-ad reissue fail."""
    from app.modules.builds.correction_models import VerifiedReplacement
    from app.modules.builds.submissions import get_submission

    env = replacement_env
    env.ad = env.original_ad.model_copy(update={"name": "approved reissue ad"})
    env.group = env.original_group
    with Session(env.engine) as session:
        sibling = session.get(ExecutionStep, env.sibling_id)
        sibling.status, sibling.phase = "SUCCEEDED", "DONE"
        sibling.remote_id = "successful-sibling-ad"
        session.add(sibling)
        session.commit()

    def rows(path, _query):
        if path.endswith("/smart_plus/ad/get/"):
            return [
                {
                    **encode_intent(env.ad),
                    "smart_plus_ad_id": "replacement-ad",
                    "operation_status": "ENABLE",
                }
            ]
        if path.endswith("/smart_plus/adgroup/get/"):
            return [
                {
                    **encode_intent(env.group),
                    "adgroup_id": "group-parent",
                }
            ]
        if path.endswith("/adgroup/get/"):
            return [
                {
                    "advertiser_id": env.ad.advertiser_id,
                    "adgroup_id": "group-parent",
                    "operation_status": "ENABLE",
                }
            ]
        raise AssertionError(path)

    calls = wire(monkeypatch, rows)
    identity = invoke(
        env,
        replacement_adgroup_id="group-parent",
        ad_intent=env.ad,
        adgroup_intent=env.group,
    )

    with Session(env.engine) as session:
        source = session.get(ExecutionStep, env.ids["AD"])
        sibling = session.get(ExecutionStep, env.sibling_id)
        link = session.get(VerifiedReplacement, identity)
        summary = get_submission(
            session, context=env.context, submission_id=env.submission_id
        )
        assert source.status == "UNKNOWN" and source.remote_id is None
        assert sibling.status == "SUCCEEDED" and sibling.remote_id is not None
        assert link.original_adgroup_id == link.remote_adgroup_id == "group-parent"
        assert link.verification["original_group_status"]["operation_status"] == "ENABLE"
        assert summary.corrected_ad_count == 1
        assert summary.unknown.ad_count == 0
    assert len(calls) == 3


def test_verified_replacement_resolves_business_result_and_preserves_attempt(
    replacement_env, monkeypatch
):
    from app.modules.builds.submission_catalog import (
        get_submission_ads,
        list_submissions,
    )
    from app.modules.builds.submissions import get_submission, get_submission_steps

    env = replacement_env
    calls = wire(monkeypatch, responses(env))
    identity = invoke(env)
    assert invoke(env) == identity
    assert len(calls) == 4  # 幂等重放无需重复平台读取。
    with Session(env.engine) as session:
        assert (
            get_submission(
                session, context=env.context, submission_id=env.submission_id
            ).status
            == "NEEDS_REVIEW"
        )
    wire(
        monkeypatch,
        lambda path, query: [
            dict(row, smart_plus_ad_id="replacement-ad-2")
            if "smart_plus_ad_id" in row
            else row
            for row in responses(env)(path, query)
        ],
    )
    invoke(
        env,
        source_step_id=env.sibling_id,
        request_id=uuid4(),
        replacement_ad_id="replacement-ad-2",
    )
    with Session(env.engine) as session:
        source = session.get(ExecutionStep, env.ids["AD"])
        assert (source.status, source.remote_id, source.request_body_digest) == (
            "UNKNOWN",
            None,
            env.source_digest,
        )
        assert session.get(ExecutionStep, env.readback_id).status == "FAILED"
        summary = get_submission(
            session, context=env.context, submission_id=env.submission_id
        )
        assert summary.status == "COMPLETED"
        assert summary.corrected_ad_count == 2
        assert summary.unknown.ad_count == summary.pending.ad_count == 0
        assert summary.succeeded.ad_count == summary.submitted.ad_count
        assert not summary.recovery.can_reconcile
        page = list_submissions(
            session, context=env.context, bc_id=source.bc_id, status_group="completed"
        )
        item = next(
            item for item in page.items if item.submission_id == env.submission_id
        )
        assert item.corrected_ad_count == 2 and item.unknown.ad_count == 0
        ads = get_submission_ads(
            session,
            context=env.context,
            submission_id=env.submission_id,
            unit_id=source.unit_id,
            group_id=source.group_id,
        )
        ad = next(ad for ad in ads.items if ad.step.step_id == source.id)
        assert ad.step.status == "UNKNOWN"
        assert ad.step.correction.remote_id == "replacement-ad"
        issues = get_submission_steps(
            session,
            context=env.context,
            submission_id=env.submission_id,
            result="UNKNOWN",
        )
        assert source.id not in {item.step_id for item in issues.items}


@pytest.mark.parametrize(
    "case",
    [
        "disabled_ad",
        "enabled_old_group",
        "mismatch",
        "changed_copy",
        "changed_group",
        "wrong_digest",
        "wrong_tenant",
        "wrong_actor",
    ],
)
def test_unverified_or_unscoped_correction_never_resolves(
    replacement_env, monkeypatch, case
):
    from dataclasses import replace

    from app.modules.builds.correction_models import VerifiedReplacement

    env = replacement_env
    wire(
        monkeypatch,
        responses(
            env,
            ad_status="DISABLE" if case == "disabled_ad" else "ENABLE",
            old_status="ENABLE" if case == "enabled_old_group" else "DISABLE",
            mismatch=case == "mismatch",
        ),
    )
    changes = {}
    if case == "changed_copy":
        changes["ad_intent"] = env.ad.model_copy(update={"text": "not approved"})
    if case == "changed_group":
        changes["adgroup_intent"] = env.group.model_copy(
            update={"minis_id": "different-mini"}
        )
    if case == "wrong_digest":
        changes["source_request_digest"] = "0" * 64
    if case == "wrong_tenant":
        changes["context"] = replace(env.context, tenant_id=uuid4())
    if case == "wrong_actor":
        changes["context"] = replace(env.context, actor_id=uuid4())
    with pytest.raises(DomainError):
        invoke(env, **changes)
    with Session(env.engine) as session:
        assert not session.exec(select(VerifiedReplacement)).all()
        assert session.get(ExecutionStep, env.ids["AD"]).status == "UNKNOWN"


def test_duplicate_mapping_cannot_reassign_a_verified_remote_ad(
    replacement_env, monkeypatch
):
    env = replacement_env
    wire(monkeypatch, responses(env))
    identity = invoke(env)
    with pytest.raises(DomainError):
        invoke(env, request_id=uuid4(), replacement_ad_id="different-ad")
    with pytest.raises(DomainError):
        invoke(env, request_id=uuid4(), source_step_id=env.sibling_id)
    assert invoke(env) == identity


def test_verified_correction_is_append_only_and_recovery_cannot_replay(
    replacement_env, monkeypatch
):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from app.modules.builds import reconciliation
    from app.modules.builds.correction_models import VerifiedReplacement
    from app.modules.builds.dispatch import repair_execution

    env = replacement_env
    calls = wire(monkeypatch, responses(env))
    identity = invoke(env)
    with Session(env.engine) as session:
        row = session.get(VerifiedReplacement, identity)
        assert row.verification["ad"]["remote_id"] == "replacement-ad"
        assert (
            row.verification["original_group_status"]["operation_status"] == "DISABLE"
        )
        assert row.source_attempt_id is not None
        before = session.get(ExecutionStep, env.ids["AD"]).model_dump()
    for operation in (
        "UPDATE build_verified_replacement SET remote_ad_id='forged' WHERE id=:id",
        "DELETE FROM build_verified_replacement WHERE id=:id",
    ):
        with Session(env.engine) as session, pytest.raises(DBAPIError):
            session.execute(text(operation), {"id": identity})
    result = reconciliation.process_reconciliation(
        database_engine=env.engine,
        redis_client=env.redis,
        context=env.context,
        step_id=env.ids["AD"],
        revision=0,
    )
    assert result.state == "VERIFIED_REPLACEMENT" and not result.needs_more
    repair_execution(database_engine=env.engine)
    with Session(env.engine) as session:
        assert session.get(ExecutionStep, env.ids["AD"]).model_dump() == before
    assert len(calls) == 4


def delayed_http_wire(env, monkeypatch, delay):
    import time
    from urllib.parse import urlsplit

    import urllib3

    from tests.integrations.tiktok.build_wire import BuildWire

    server = BuildWire("OFFICIAL_API")
    original_get = server.server.RequestHandlerClass.do_GET
    original_request = urllib3.PoolManager.request
    waited = False

    def delayed_get(handler):
        nonlocal waited
        if not waited:
            waited = True
            time.sleep(delay)
        try:
            original_get(handler)
        except (BrokenPipeError, ConnectionResetError):
            # 超时用例的客户端按截止时间断开；服务端完成写入不改变测试结论。
            return

    def local_request(pool, method, url, **kwargs):
        assert method == "GET"
        return original_request(
            pool, method, server.endpoint + urlsplit(url).path, **kwargs
        )

    server.server.RequestHandlerClass.do_GET = delayed_get
    monkeypatch.setattr(urllib3.PoolManager, "request", local_request)
    server.enqueue_readback(
        "AD", [{**encode_intent(env.ad), "smart_plus_ad_id": "replacement-ad"}], 1, 1
    )
    server.enqueue_readback(
        "ADGROUP",
        [{**encode_intent(env.group), "adgroup_id": "replacement-group"}],
        1,
        1,
    )
    for identity, status in (
        ("replacement-group", "ENABLE"),
        ("group-parent", "DISABLE"),
    ):
        server.enqueue_readback(
            "ADGROUP_STATUS",
            [
                {
                    "advertiser_id": env.ad.advertiser_id,
                    "adgroup_id": identity,
                    "operation_status": status,
                }
            ],
            1,
            1,
        )
    return server


def test_correction_survives_valid_remote_reads_longer_than_callback_sql_window(
    replacement_env, monkeypatch
):
    env = replacement_env
    server = delayed_http_wire(env, monkeypatch, 5.1)
    try:
        identity = invoke(env)
        assert invoke(env) == identity
        assert len(server.business_calls()) == 4
    finally:
        server.close()


def test_correction_total_deadline_does_not_publish_partial_verification(
    replacement_env, monkeypatch
):
    from app.modules.builds.correction_models import VerifiedReplacement

    env = replacement_env
    server = delayed_http_wire(env, monkeypatch, 3.1)
    try:
        with pytest.raises(DomainError):
            invoke(env, task_deadline=datetime.now(UTC) + timedelta(seconds=3))
        with Session(env.engine) as session:
            assert not session.exec(select(VerifiedReplacement)).all()
            assert session.get(ExecutionStep, env.ids["AD"]).status == "UNKNOWN"
    finally:
        server.close()
