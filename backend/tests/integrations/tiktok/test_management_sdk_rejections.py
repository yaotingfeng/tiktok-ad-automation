"""真实官方SDK的序列化/反序列化，只有HTTP request边界使用本地替身。"""

import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.integrations.tiktok.adapters.sdk_management import SdkManagementOperations
from app.integrations.tiktok.contracts.ads import EntityRef
from app.integrations.tiktok.contracts.common import RemoteCallError
from app.integrations.tiktok.contracts.management import ManagementCommand
from app.integrations.tiktok.sdk import official_client


def _command(index, *, budget=False):
    return ManagementCommand(
        EntityRef(uuid4(), "adv", "adgroup" if budget else "campaign", str(index)),
        "budget" if budget else "status",
        {"ad_type": "SMART_PLUS" if budget else "REGULAR"},
        {"budget": "100"} if budget else {"status": "DISABLE"},
    )


def _invoke(monkeypatch, *, mode, raw, headers=None):
    observations = []
    calls = []

    @contextmanager
    def scope(*_args):
        try:
            yield
        except Exception as error:
            observations.append(error)
            raise

    with official_client(access_token="synthetic-test-token") as client:

        def request(*args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(
                data=json.dumps(raw).encode(),
                status=200,
                getheaders=lambda: headers or {},
            )

        monkeypatch.setattr(client, "request", request)
        adapter = SdkManagementOperations(
            client,
            request_scope=scope,
            management_scope=scope,
            deadline=datetime.now(UTC) + timedelta(seconds=30),
            capability_check=lambda *_args: None,
        )
        first = _command(1, budget=mode == "budget_batch")
        from dataclasses import replace

        second = replace(first, ref=replace(first.ref, remote_id="2"))
        if mode == "single":
            receipts = (adapter.apply(first),)
        else:
            receipts = adapter.apply_batch((first, second))
    return receipts, observations, calls


@pytest.mark.parametrize("mode", ["single", "status_batch", "budget_batch"])
@pytest.mark.parametrize(
    "message,headers,reason,delay",
    [
        ("QPM exceeded", {"Retry-After": "2"}, "qpm", 300_000),
        ("QPS exceeded", {"Retry-After": "7"}, "qps", 7_000),
        ("Unknown quota type", {}, "40100", 300_000),
        ("Unknown quota type", {"Retry-After": "9"}, "retry_after", 9_000),
    ],
)
def test_real_sdk_quota_response_preserves_rejection_and_scope_cooldown(
    monkeypatch, mode, message, headers, reason, delay
):
    receipts, observations, calls = _invoke(
        monkeypatch,
        mode=mode,
        raw={
            "code": 40100,
            "message": message,
            "request_id": "rate-request",
            "data": {},
        },
        headers=headers,
    )
    assert {r.outcome for r in receipts} == {"REJECTED"}
    assert {r.evidence.remote_code for r in receipts} == {40100}
    assert {r.request_id for r in receipts} == {"rate-request"}
    assert {r.retry_after_ms for r in receipts} == {delay}
    assert not any(r.retryable for r in receipts)
    assert len(calls) == 1
    assert len(observations) == 1
    assert observations[0].admission_source == "provider"
    assert observations[0].admission_reason == reason
    assert observations[0].retry_after_ms == delay


@pytest.mark.parametrize("mode", ["single", "status_batch", "budget_batch"])
def test_real_sdk_qpd_rejection_waits_until_next_utc_day(monkeypatch, mode):
    before = datetime.now(UTC)
    receipts, observations, calls = _invoke(
        monkeypatch,
        mode=mode,
        raw={
            "code": 40100,
            "message": "QPD exceeded",
            "request_id": "day-request",
            "data": {},
        },
    )
    after = datetime.now(UTC)
    tomorrow = (before + timedelta(days=1)).replace(
        hour=0, minute=0, second=5, microsecond=0
    )
    assert {r.outcome for r in receipts} == {"REJECTED"}
    assert all(
        int((tomorrow - after).total_seconds() * 1000)
        <= r.retry_after_ms
        <= int((tomorrow - before).total_seconds() * 1000)
        for r in receipts
    )
    assert observations[0].admission_reason == "qpd"
    assert len(calls) == 1


@pytest.mark.parametrize("mode", ["single", "status_batch", "budget_batch"])
def test_real_sdk_nonzero_business_envelope_is_not_lost_as_unknown(monkeypatch, mode):
    receipts, _observations, calls = _invoke(
        monkeypatch,
        mode=mode,
        raw={
            "code": 40002,
            "message": "private diagnostic must not enter receipt",
            "request_id": "reject-request",
            "data": {},
        },
    )
    assert {r.outcome for r in receipts} == {"REJECTED"}
    assert {r.evidence.remote_code for r in receipts} == {40002}
    assert {r.request_id for r in receipts} == {"reject-request"}
    assert not any(r.retryable for r in receipts)
    assert "private diagnostic" not in str(receipts)
    assert len(calls) == 1


@pytest.mark.parametrize("mode", ["single", "status_batch", "budget_batch"])
def test_real_sdk_malformed_business_code_remains_unknown(monkeypatch, mode):
    try:
        receipts, _observations, _calls = _invoke(
            monkeypatch,
            mode=mode,
            raw={
                "code": "40100",
                "message": "QPM exceeded",
                "request_id": "malformed",
                "data": {},
            },
        )
    except RemoteCallError as error:
        assert mode == "single" and error.effect == "UNKNOWN"
    else:
        assert {r.outcome for r in receipts} == {"UNKNOWN"}


@pytest.mark.parametrize("mode", ["single", "status_batch", "budget_batch"])
def test_sdk_rejection_without_success_data_retains_request_evidence(monkeypatch, mode):
    receipts, _observations, calls = _invoke(
        monkeypatch,
        mode=mode,
        raw={"code": 40002, "message": "Invalid parameter", "request_id": "no-data"},
    )
    assert {r.outcome for r in receipts} == {"REJECTED"}
    assert {r.evidence.remote_code for r in receipts} == {40002}
    assert {r.request_id for r in receipts} == {"no-data"}
    assert len(calls) == 1


@pytest.mark.parametrize("batch", [False, True])
def test_send_exception_never_reuses_stale_rejected_response(monkeypatch, batch):
    @contextmanager
    def scope(*_args):
        yield

    with official_client(access_token="synthetic-test-token") as client:
        client.last_response = SimpleNamespace(
            data=b'{"code":40002,"request_id":"stale","data":{}}'
        )

        def request(*_args, **_kwargs):
            raise TimeoutError("wire failed without a new response")

        monkeypatch.setattr(client, "request", request)
        adapter = SdkManagementOperations(
            client,
            request_scope=scope,
            management_scope=scope,
            deadline=datetime.now(UTC) + timedelta(seconds=30),
            capability_check=lambda *_args: None,
        )
        if batch:
            receipt = adapter.apply_batch((_command(1),))[0]
            assert receipt.outcome == "UNKNOWN"
            assert receipt.request_id is None
            assert receipt.evidence.remote_code is None
        else:
            with pytest.raises(RemoteCallError) as caught:
                adapter.apply(_command(1))
            assert caught.value.effect == "UNKNOWN"
            assert caught.value.evidence.request_id is None
            assert caught.value.evidence.remote_code is None


@pytest.mark.parametrize("budget", [False, True])
def test_qpm_sdk_rejection_cools_same_physical_endpoint_across_tenants(
    monkeypatch, redis_client, budget
):
    """真实SDK→真实Redis准入→另一租户被冷却；第三个物理端点独立可用。"""
    from dataclasses import replace

    from app.integrations.tiktok.adapters.management_batches import batch_endpoint
    from app.integrations.tiktok.sdk import AccountAdmissionDeferred
    from app.jobs.admission import AdmissionPolicy, admission_keys, admitted_scope

    app_scope = f"test-sdk-management-cooldown-{uuid4()}"
    policy = AdmissionPolicy(
        app_max_inflight=10,
        endpoint_max_inflight=10,
        tenant_max_inflight=10,
        advertiser_max_inflight=10,
        app_calls_per_window=100,
        endpoint_calls_per_window=100,
        window_ms=1000,
        lease_ms=60000,
    )
    first = _command(1, budget=budget)
    second = replace(
        first, ref=EntityRef(uuid4(), "another-account", first.ref.kind, "2")
    )
    third = ManagementCommand(
        EntityRef(second.ref.tenant_id, "another-account", "ad", "3"),
        "status",
        {"ad_type": "REGULAR"},
        {"status": "DISABLE"},
    )
    calls = []
    denied = []
    endpoint = batch_endpoint(first)
    owned_keys = admission_keys(
        app_scope, endpoint, first.ref.tenant_id, first.ref.advertiser_id
    )
    redis_prefix = owned_keys[0].removesuffix(":rate")

    def invoke(command, raw):
        @contextmanager
        def scope(advertiser, _operation, _kind, _deadline, physical_endpoint):
            try:
                with admitted_scope(
                    redis_client,
                    app_scope=app_scope,
                    endpoint=physical_endpoint,
                    rate_group=physical_endpoint,
                    tenant_id=command.ref.tenant_id,
                    advertiser_id=advertiser,
                    policy=policy,
                    denied_error=AccountAdmissionDeferred,
                ):
                    yield
            except AccountAdmissionDeferred as error:
                denied.append(error)
                raise

        with official_client(access_token="synthetic-test-token") as client:

            def request(*args, **kwargs):
                calls.append((args, kwargs))
                return SimpleNamespace(
                    data=json.dumps(raw).encode(),
                    status=200,
                    getheaders=lambda: {"Retry-After": "2"},
                )

            monkeypatch.setattr(client, "request", request)
            adapter = SdkManagementOperations(
                client,
                request_scope=scope,
                management_scope=scope,
                deadline=datetime.now(UTC) + timedelta(seconds=30),
                capability_check=lambda *_args: None,
            )
            return adapter.apply_batch((command,))[0]

    try:
        rejected = invoke(
            first,
            {
                "code": 40100,
                "message": "QPM exceeded",
                "request_id": "qpm-shared",
                "data": {},
            },
        )
        assert rejected.outcome == "REJECTED"
        assert rejected.evidence.remote_code == 40100
        assert rejected.request_id == "qpm-shared"
        assert rejected.retry_after_ms == 300_000 and not rejected.retryable
        assert 295_000 <= redis_client.pttl(owned_keys[1] + ":cooldown") <= 300_000
        assert denied[0].admission_source == "provider"
        assert denied[0].admission_reason == "qpm"

        blocked = invoke(second, {"code": 0, "data": {}})
        assert blocked.outcome == "NOT_SENT" and blocked.retryable
        assert 295_000 <= blocked.retry_after_ms <= 300_000
        assert denied[1].admission_source == "local"
        assert denied[1].admission_reason == "provider_cooldown"
        assert len(calls) == 1
        # 被本地冷却挡住的另租户不消耗账户/租户并发或物理接口请求计数。
        second_keys = admission_keys(
            app_scope, endpoint, second.ref.tenant_id, second.ref.advertiser_id
        )
        assert redis_client.zcard(second_keys[1]) == 1
        assert all(redis_client.zcard(second_keys[index]) == 0 for index in (4, 5))

        accepted = invoke(
            third,
            {"code": 0, "request_id": "other-endpoint", "data": {"ad_ids": ["3"]}},
        )
        assert accepted.outcome == "ACCEPTED"
        assert len(calls) == 2
        assert batch_endpoint(third) != endpoint
    finally:
        keys = list(redis_client.scan_iter(match=f"{redis_prefix}:*"))
        if keys:
            redis_client.delete(*keys)
