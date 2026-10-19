"""只在本地临时 PostgreSQL/Redis 复现；TikTok 传输由内存替身替代。"""

from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlmodel import Session, select

from app.core.config import settings
from app.core.db import engine
from app.core.errors import DomainError
from app.integrations.tiktok.adapters import sdk_management
from app.integrations.tiktok.admission import admit_tiktok_call
from app.integrations.tiktok.sdk import AccountAdmissionDeferred
from app.jobs.admission import AdmissionPolicy, admit_call, release_call
from app.modules.ad_management.execution import execute_item
from app.modules.ad_management.models import (
    ManagementRequestAttempt,
    ManagementTaskItem,
)
from tests.modules.ad_management.test_execution import _committed_item
from tests.modules.ad_management.test_execution import c5_env as c5_env


@pytest.mark.parametrize(
    "failure",
    [
        "quota",
        "quota_released",
        "quota_conflict",
        "pre_send_conflict",
        "timeout",
        "accepted",
    ],
)
def test_recorded_effect(c5_env, redis_client, monkeypatch, failure):
    context, _task_id, item_id = _committed_item(
        c5_env, original="ENABLE", final="DISABLE", operation="set_material_status"
    )
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", False)
    monkeypatch.setattr(
        "app.modules.ad_management.execution._redis", lambda: redis_client
    )
    route = c5_env[3]
    scope_name = f"test-rule-audit-{uuid4()}"
    policy = AdmissionPolicy(
        app_max_inflight=1,
        endpoint_max_inflight=5,
        tenant_max_inflight=5,
        advertiser_max_inflight=5,
        app_calls_per_window=20,
        endpoint_calls_per_window=20,
        window_ms=1000,
        lease_ms=60000,
    )
    sent = []
    errors = []
    lease_id = uuid4()
    if failure.startswith("quota"):
        # 模拟报表占用同一应用的在途额度，实际走 Redis Lua 准入。
        assert admit_call(
            redis_client,
            app_scope=scope_name,
            endpoint="reports.material_breakdown",
            tenant_id=context.tenant_id,
            advertiser_id="other-test-account",
            lease_id=lease_id,
            policy=policy,
        ).granted

    class FakeAdApi:
        def __init__(self, client):
            pass

        def smart_plus_ad_material_status_update(self, *args, **kwargs):
            sent.append(kwargs["body"])
            if failure == "timeout":
                raise TimeoutError("synthetic transport timeout")
            return SimpleNamespace(get=lambda: None)

    monkeypatch.setattr(sdk_management.sdk, "AdApi", FakeAdApi)

    @contextmanager
    def gateway(**kw):
        @contextmanager
        def scope(adv, operation, _kind, _deadline):
            try:
                with admit_tiktok_call(
                    redis_client,
                    route=route,
                    advertiser_id=adv,
                    operation=f"management.{operation}",
                    scope=scope_name,
                    policy=policy,
                ):
                    yield
            except AccountAdmissionDeferred as error:
                errors.append(error.code)
                raise

        def before_send():
            kw["before_request"]()
            if failure == "pre_send_conflict":
                errors.append("management_original_changed")
                raise DomainError(
                    "management_original_changed", "synthetic frozen value changed"
                )

        client = SimpleNamespace(
            default_headers={"Access-Token": "synthetic"},
            last_response=SimpleNamespace(
                data=b'{"code":0,"data":{},"request_id":"synthetic-request"}'
            ),
        )
        adapter = sdk_management.SdkManagementOperations(
            client,
            request_scope=scope,
            management_scope=scope,
            capability_check=lambda *_: kw["before_request"](),
            before_send=before_send,
            deadline=kw["task_deadline"],
        )
        yield SimpleNamespace(management=adapter)

    monkeypatch.setattr(
        "app.modules.ad_management.execution.open_tiktok_gateway", gateway
    )
    execute_item(engine, item_id)
    with Session(engine) as session:
        item = session.get(ManagementTaskItem, item_id)
        attempt = session.exec(
            select(ManagementRequestAttempt).where(
                ManagementRequestAttempt.task_item_id == item_id
            )
        ).one()
        if failure.startswith("quota"):
            assert not sent
            assert item.execution_result == "NOT_SENT"
            assert attempt.response["reason"] == "admission_deferred"
            assert attempt.retryable
            from app.jobs.models import PendingDispatch

            retry = session.exec(
                select(PendingDispatch).where(
                    PendingDispatch.task_key == f"ad-management-retry:{item_id}:1"
                )
            ).one()
            assert retry.available_at > attempt.request_at
            # 提前的重复投递不能绕开额度等待，也不能多记一次尝试。
            execute_item(engine, item_id)
            session.expire_all()
            assert (
                len(
                    session.exec(
                        select(ManagementRequestAttempt).where(
                            ManagementRequestAttempt.task_item_id == item_id
                        )
                    ).all()
                )
                == 1
            )
            if failure != "quota":
                release_call(
                    redis_client,
                    app_scope=scope_name,
                    endpoint="reports.material_breakdown",
                    tenant_id=context.tenant_id,
                    advertiser_id="other-test-account",
                    lease_id=lease_id,
                )
            if failure == "quota_conflict":
                failure = "pre_send_conflict"
            for attempt_no in (2, 3):
                # 到期由时钟推进模拟；真实 Redis 仍被占用，验证有界延期而非忙循环。
                last = session.exec(
                    select(ManagementRequestAttempt)
                    .where(ManagementRequestAttempt.task_item_id == item_id)
                    .order_by(ManagementRequestAttempt.attempt.desc())
                ).first()
                monkeypatch.setattr(
                    "app.modules.ad_management.execution._now",
                    lambda last=last: (
                        datetime.fromisoformat(last.response["retry_at"])
                        + timedelta(seconds=1)
                    ),
                )
                execute_item(engine, item_id)
                session.expire_all()
                latest = session.exec(
                    select(ManagementRequestAttempt)
                    .where(ManagementRequestAttempt.task_item_id == item_id)
                    .order_by(ManagementRequestAttempt.attempt.desc())
                ).first()
                assert latest.attempt == attempt_no
                if failure == "quota_released":
                    assert latest.outcome == "ACCEPTED" and len(sent) == 1
                    break
                if failure == "pre_send_conflict":
                    assert (
                        latest.outcome == "NOT_SENT"
                        and not latest.retryable
                        and not sent
                    )
                    assert (
                        session.get(ManagementTaskItem, item_id).execution_result
                        == "CONFLICT"
                    )
                    break
            else:
                assert not latest.retryable and latest.response["retry_exhausted"]
                execute_item(engine, item_id)
                assert not sent
        elif failure == "pre_send_conflict":
            assert not sent
            assert item.execution_result == "CONFLICT"
            assert attempt.outcome == "NOT_SENT"
            assert attempt.response["reason"] == "management_original_changed"
            assert not attempt.retryable
        elif failure == "timeout":
            assert len(sent) == 1 and item.execution_result == "UNKNOWN"
            assert attempt.response["reason"] == "management_result_unknown"
            execute_item(engine, item_id)
            assert len(sent) == 1
        else:
            assert len(sent) == 1 and item.execution_result == "ACCEPTED"
