"""创建限流必须穿过真实SDK与准入scope；只替换HTTP传输，不创建真实广告。"""

import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.integrations.tiktok.adapters.sdk_builds import ApiBuildOperations
from app.integrations.tiktok.contracts.common import RemoteCallError
from app.integrations.tiktok.platform_quotas import sdk_endpoint
from app.integrations.tiktok.sdk import AccountAdmissionDeferred, official_client
from app.jobs.admission import AdmissionPolicy, admission_keys, admitted_scope
from app.modules.builds.request_compiler import decode_intent
from tests.contracts.test_tiktok_build_contract import build_bodies


def _body(kind):
    body = build_bodies()[kind]
    if kind == "ADGROUP":
        # 合同往返fixture刻意含无法无损编码成JSON number的ROAS；传输测试用精确值。
        body["roas_bid"] = "1.25"
    return body


@pytest.mark.parametrize("kind", ["CAMPAIGN", "ADGROUP", "AD", "CTA"])
@pytest.mark.parametrize(
    "message,reason,delay",
    [
        ("QPM exceeded", "qpm", 300_000),
        ("QPD exceeded", "qpd", None),
        ("QPS exceeded", "qps", 7_000),
        ("Unknown quota", "retry_after", 7_000),
    ],
)
def test_create_quota_keeps_unknown_effect_and_rate_evidence(
    monkeypatch, kind, message, reason, delay
):
    seen = []

    @contextmanager
    def scope(*_args):
        try:
            yield
        except AccountAdmissionDeferred as error:
            seen.append(error)
            raise

    with official_client(access_token="synthetic-token") as client:
        calls = []

        def request(*args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(
                data=json.dumps(
                    {
                        "code": 40100,
                        "message": message,
                        "request_id": "quota-create",
                        "data": {},
                    }
                ).encode(),
                status=200,
                getheaders=lambda: {"Retry-After": "7"},
            )

        monkeypatch.setattr(client, "request", request)
        adapter = ApiBuildOperations(
            client,
            request_scope=scope,
            deadline=datetime.now(UTC) + timedelta(seconds=30),
        )
        with pytest.raises(RemoteCallError) as caught:
            adapter.create(attempt_id=uuid4(), intent=decode_intent(kind, _body(kind)))
        assert caught.value.effect == "UNKNOWN"
        assert caught.value.evidence.remote_code == 40100
        assert caught.value.evidence.request_id == "quota-create"
        if delay is None:
            now = datetime.now(UTC)
            tomorrow = (now + timedelta(days=1)).replace(
                hour=0, minute=0, second=5, microsecond=0
            )
            remaining_ms = int((tomorrow - now).total_seconds() * 1000)
            assert remaining_ms <= caught.value.retry_after_ms <= remaining_ms + 2000
        else:
            assert caught.value.retry_after_ms == delay
        assert not caught.value.retryable
        assert len(calls) == len(seen) == 1
        assert seen[0].admission_source == "provider"
        assert seen[0].admission_reason == reason


def test_create_qpm_shared_cooldown_rejects_another_tenant_before_http(
    monkeypatch, redis_client
):
    app_scope = f"test-create-quota-{uuid4()}"
    tenant = uuid4()
    other_tenant = uuid4()
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
    endpoint = sdk_endpoint("build.create_campaign")
    owned_keys = admission_keys(app_scope, endpoint, tenant, "advertiser-one")
    prefix = owned_keys[0].removesuffix(":rate")
    calls = []

    def create(kind, tenant_id, advertiser, raw):
        @contextmanager
        def scope(advertiser_id, operation, _deadline):
            with admitted_scope(
                redis_client,
                app_scope=app_scope,
                endpoint=sdk_endpoint(operation),
                rate_group=sdk_endpoint(operation),
                tenant_id=tenant_id,
                advertiser_id=advertiser_id,
                policy=policy,
                denied_error=AccountAdmissionDeferred,
            ):
                yield

        with official_client(access_token="synthetic-token") as client:

            def request(*args, **kwargs):
                calls.append((args, kwargs))
                return SimpleNamespace(
                    data=json.dumps(raw).encode(), status=200, getheaders=lambda: {}
                )

            monkeypatch.setattr(client, "request", request)
            adapter = ApiBuildOperations(
                client,
                request_scope=scope,
                deadline=datetime.now(UTC) + timedelta(seconds=30),
            )
            body = {**_body(kind), "advertiser_id": advertiser}
            return adapter.create(attempt_id=uuid4(), intent=decode_intent(kind, body))

    try:
        with pytest.raises(RemoteCallError) as caught:
            create(
                "CAMPAIGN",
                tenant,
                "advertiser-one",
                {
                    "code": 40100,
                    "request_id": "cooldown-create",
                    "message": "QPM exceeded",
                    "data": {},
                },
            )
        assert caught.value.effect == "UNKNOWN"
        assert caught.value.evidence.remote_code == 40100
        assert caught.value.evidence.request_id == "cooldown-create"
        assert caught.value.retry_after_ms == 300_000
        assert 295_000 <= redis_client.pttl(owned_keys[1] + ":cooldown") <= 300_000
        with pytest.raises(AccountAdmissionDeferred) as blocked:
            create(
                "CAMPAIGN",
                other_tenant,
                "advertiser-two",
                {"code": 0, "data": {"campaign_id": "unexpected"}},
            )
        assert blocked.value.admission_reason == "provider_cooldown"
        assert blocked.value.admission_source == "local"
        assert len(calls) == 1
        result = create(
            "ADGROUP",
            other_tenant,
            "advertiser-two",
            {
                "code": 0,
                "request_id": "other-endpoint",
                "data": {"adgroup_id": "group-new", "operation_status": "ENABLE"},
            },
        )
        assert result.remote_id == "group-new"
        assert result.evidence.request_id == "other-endpoint"
        assert len(calls) == 2
    finally:
        keys = list(redis_client.scan_iter(match=f"{prefix}:*"))
        if keys:
            redis_client.delete(*keys)


@pytest.mark.parametrize(
    "kind,code,effect",
    [
        ("ADGROUP", 40002, "REJECTED_NO_EFFECT"),
        ("AD", 40002, "REJECTED_NO_EFFECT"),
        ("AD", 51002, "REJECTED_NO_EFFECT"),
        ("CAMPAIGN", 40002, "UNKNOWN"),
    ],
)
def test_existing_create_no_effect_contract_stays_unchanged(
    monkeypatch, kind, code, effect
):
    @contextmanager
    def scope(*_args):
        yield

    with official_client(access_token="synthetic-token") as client:
        monkeypatch.setattr(
            client,
            "request",
            lambda *_args, **_kwargs: SimpleNamespace(
                data=json.dumps(
                    {"code": code, "request_id": "rejected", "data": {}}
                ).encode(),
                status=200,
                getheaders=lambda: {},
            ),
        )
        adapter = ApiBuildOperations(
            client,
            request_scope=scope,
            deadline=datetime.now(UTC) + timedelta(seconds=30),
        )
        with pytest.raises(RemoteCallError) as caught:
            adapter.create(attempt_id=uuid4(), intent=decode_intent(kind, _body(kind)))
        assert caught.value.effect == effect
        assert caught.value.evidence.remote_code == code
        assert caught.value.evidence.request_id == "rejected"


def test_create_timeout_cannot_reuse_old_quota_response(monkeypatch):
    @contextmanager
    def scope(*_args):
        yield

    with official_client(access_token="synthetic-token") as client:
        client.last_response = SimpleNamespace(
            data=b'{"code":40100,"request_id":"stale","data":{}}'
        )

        def request(*_args, **_kwargs):
            raise TimeoutError("no response")

        monkeypatch.setattr(client, "request", request)
        adapter = ApiBuildOperations(
            client,
            request_scope=scope,
            deadline=datetime.now(UTC) + timedelta(seconds=30),
        )
        with pytest.raises(RemoteCallError) as caught:
            adapter.create(
                attempt_id=uuid4(),
                intent=decode_intent("CAMPAIGN", build_bodies()["CAMPAIGN"]),
            )
        assert caught.value.effect == "UNKNOWN"
        assert caught.value.evidence.request_id is None
        assert caught.value.evidence.remote_code is None
        assert caught.value.retry_after_ms is None
