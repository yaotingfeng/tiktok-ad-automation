# backend/tests/integrations/tiktok/test_sdk_report_reader.py
import json
from datetime import date

import pytest

from app.core.errors import DomainError
from app.integrations.tiktok.adapters.sdk_report_reader import SdkReportReader
from tests.integrations.tiktok.gateway_support import database_engine as database_engine
from tests.integrations.tiktok.gateway_support import gateway
from tests.integrations.tiktok.gateway_support import gateway_case as gateway_case
from tests.integrations.tiktok.gateway_support import gateway_wire as gateway_wire
from tests.modules.accounts.conftest import app_config as app_config
from tests.modules.accounts.conftest import policy as policy


class _Recorder:
    def __init__(self, body):
        self.body, self.calls = body, []

    def __call__(self, operation, advertiser_id, arguments):
        self.calls.append((operation, advertiser_id, arguments))
        return self.body


def _reader(body):
    recorder = _Recorder(body)
    return SdkReportReader(send=recorder), recorder


PAGE = {
    "list": [],
    "page_info": {"page": 1, "page_size": 1000, "total_page": 1, "total_number": 0},
}


def test_ad_day_payload_always_includes_status_all():
    reader, rec = _reader(PAGE)
    reader.ad_day(
        advertiser_ids=("a", "b"),
        start=date(2026, 10, 5),
        end=date(2026, 10, 5),
        page=1,
    )
    op, adv, args = rec.calls[0]
    assert op == "reports.integrated" and adv == "a"
    assert args["advertiser_ids"] == ["a", "b"] and args["data_level"] == "AUCTION_AD"
    assert (
        args["dimensions"] == ["ad_id_v2", "stat_time_day"]
        and args["page_size"] == 1000
    )
    assert {
        "field_name": "ad_status",
        "filter_type": "IN",
        "filter_value": json.dumps(["STATUS_ALL"]),
    } in args["filtering"]
    assert "campaign_automation_type" in args["metrics"]


def test_ad_day_campaign_filter_limit_30():
    reader, rec = _reader(PAGE)
    reader.ad_day(
        advertiser_ids=("a",),
        start=date(2026, 10, 5),
        end=date(2026, 10, 5),
        page=1,
        campaign_ids=tuple(str(i) for i in range(30)),
    )
    assert any(f["field_name"] == "campaign_ids" for f in rec.calls[0][2]["filtering"])
    with pytest.raises(DomainError):
        reader.ad_day(
            advertiser_ids=("a",),
            start=date(2026, 10, 5),
            end=date(2026, 10, 5),
            page=1,
            campaign_ids=tuple(str(i) for i in range(31)),
        )


def test_rejects_more_than_five_accounts_and_long_ranges():
    reader, _ = _reader(PAGE)
    with pytest.raises(DomainError):
        reader.ad_day(
            advertiser_ids=tuple("abcdef"),
            start=date(2026, 10, 1),
            end=date(2026, 10, 1),
            page=1,
        )
    with pytest.raises(DomainError):
        reader.ad_day(
            advertiser_ids=("a",), start=date(2026, 9, 1), end=date(2026, 10, 5), page=1
        )


def test_bc_spend_has_no_advertiser_scope():
    reader, rec = _reader(PAGE)
    reader.bc_spend(bc_id="bc1", start=date(2026, 9, 4), end=date(2026, 10, 5), page=1)
    op, adv, args = rec.calls[0]
    assert (
        op == "reports.bc"
        and adv is None
        and args["report_type"] == "BC"
        and args["bc_id"] == "bc1"
    )


def test_page_info_validation_and_empty_object():
    reader, _ = _reader({})
    page = reader.account_day(
        advertiser_ids=("a",), start=date(2026, 10, 5), end=date(2026, 10, 5), page=1
    )
    assert page.rows == () and page.total_number == 0
    reader, _ = _reader(
        {
            "list": [{}],
            "page_info": {
                "page": 2,
                "page_size": 1000,
                "total_page": 1,
                "total_number": 1,
            },
        }
    )
    with pytest.raises(DomainError):
        reader.account_day(
            advertiser_ids=("a",),
            start=date(2026, 10, 5),
            end=date(2026, 10, 5),
            page=2,
        )


def test_material_day_single_account():
    reader, rec = _reader(PAGE)
    reader.material_day(
        advertiser_id="a", start=date(2026, 10, 5), end=date(2026, 10, 5), page=1
    )
    op, adv, args = rec.calls[0]
    assert op == "reports.material_breakdown" and args["dimensions"] == [
        "main_material_id",
        "stat_time_day",
    ]
    assert args["page_size"] == 100
    # 平台随行返回素材名称，请求 metrics 中带上它会被 40002 拒绝。
    assert "main_material_name" not in args["metrics"]
    assert "spend" in args["metrics"]


def test_material_ad_links_reads_lifetime_ad_by_material():
    reader, rec = _reader(PAGE)
    reader.material_ad_links(advertiser_id="a", page=1)
    op, adv, args = rec.calls[0]
    # 概览按“广告 × 主素材”查全量历史，只为拿到广告内素材 ID 做对照。
    assert op == "reports.material_overview" and adv == "a"
    assert args["dimensions"] == ["smart_plus_ad_id", "main_material_id"]
    assert args["query_lifetime"] is True and "start_date" not in args
    assert "ad_material_id" in args["metrics"]


def test_material_ad_posts_filters_breakdown_by_ad():
    reader, rec = _reader(PAGE)
    reader.material_ad_posts(
        advertiser_id="a",
        ad_ids=("ad-9",),
        start=date(2026, 10, 1),
        end=date(2026, 10, 13),
        page=1,
    )
    op, adv, args = rec.calls[0]
    # 按广告筛选的拆分报表用于定位概览已不再列出的旧帖子。
    assert op == "reports.material_breakdown" and adv == "a"
    assert args["dimensions"] == ["main_material_id"]
    assert args["filtering"] == {"smart_plus_ad_ids": ["ad-9"]}
    assert (args["start_date"], args["end_date"]) == ("2026-10-01", "2026-10-13")


@pytest.mark.parametrize(
    "kind", ["ad", "bc", "utc", "material", "overview", "material_by_ad"]
)
def test_generated_sdk_wire_keeps_parameters_and_exact_numbers(monkeypatch, kind):
    from contextlib import contextmanager
    from datetime import UTC, datetime, timedelta

    from urllib3.response import HTTPResponse

    from app.integrations.tiktok.gateway import _sdk_report_send
    from app.integrations.tiktok.sdk import official_client

    calls = []
    admissions = []

    def wire(_pool, method, url, **kwargs):
        calls.append((method, url, dict(kwargs["fields"])))
        return HTTPResponse(
            body=b'{"code":0,"data":{"value":1.234567890123456789},"request_id":"test"}',
            status=200,
        )

    @contextmanager
    def scope(adv, operation, deadline):
        admissions.append((adv, operation, deadline))
        yield

    monkeypatch.setattr("urllib3.PoolManager.request", wire)
    deadline = datetime.now(UTC) + timedelta(seconds=30)
    with official_client(access_token="synthetic-test") as client:
        send = _sdk_report_send(client, scope, deadline)
        args = {
            "report_type": "BASIC",
            "advertiser_ids": ["a", "b"],
            "dimensions": ["ad_id_v2", "stat_time_day"],
            "metrics": ["spend"],
            "start_date": "2026-10-05",
            "end_date": "2026-10-05",
            "page": 1,
        }
        op, adv = "reports.integrated", "a"
        if kind == "bc":
            args = {
                "report_type": "BC",
                "bc_id": "bc-1",
                "dimensions": ["advertiser_id"],
                "metrics": ["spend"],
            }
            op, adv = "reports.bc", None
        elif kind == "utc":
            args["multi_adv_report_in_utc_time"] = True
        elif kind == "material":
            args.pop("report_type")
            args.pop("advertiser_ids")
            args["advertiser_id"] = "a"
            op = "reports.material_breakdown"
        elif kind == "material_by_ad":
            args = {
                "advertiser_id": "a",
                "dimensions": ["main_material_id"],
                "metrics": ["spend"],
                "filtering": {"smart_plus_ad_ids": ["ad-9"]},
                "start_date": "2026-10-01",
                "end_date": "2026-10-13",
                "page": 1,
            }
            op = "reports.material_breakdown"
        elif kind == "overview":
            args = {
                "advertiser_id": "a",
                "dimensions": ["smart_plus_ad_id", "main_material_id"],
                "metrics": ["spend", "ad_material_id"],
                "query_lifetime": True,
                "page": 1,
            }
            op = "reports.material_overview"
        assert send(op, adv, args)["value"] == "1.234567890123456789"
    assert admissions == [(adv, op, deadline)]
    method, url, fields = calls[0]
    assert method == "GET"
    if kind in {"ad", "utc"}:
        assert json.loads(fields["advertiser_ids"]) == ["a", "b"]
    if kind == "bc":
        assert fields["bc_id"] == "bc-1"
    if kind == "utc":
        assert fields["multi_adv_report_in_utc_time"] is True
    if kind == "material":
        assert url.endswith("/smart_plus/material_report/breakdown/")
    if kind == "material_by_ad":
        assert url.endswith("/smart_plus/material_report/breakdown/")
        assert json.loads(fields["filtering"]) == {"smart_plus_ad_ids": ["ad-9"]}
    if kind == "overview":
        assert url.endswith("/smart_plus/material_report/overview/")
        assert fields["query_lifetime"] is True


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
@pytest.mark.parametrize("outside", ["account", "bc"])
def test_report_gateway_rejects_scope_before_physical_send(
    database_engine, redis_client, gateway_case, gateway_wire, outside
):
    gateway_wire["sdk_data"]["data"] = PAGE
    with gateway(database_engine, redis_client, gateway_case) as client:
        with pytest.raises(DomainError):
            if outside == "account":
                client.report_reader.ad_day(
                    advertiser_ids=(gateway_case[2], "not-authorized"),
                    start=date(2026, 10, 5),
                    end=date(2026, 10, 5),
                    page=1,
                )
            else:
                client.report_reader.bc_spend(
                    bc_id="outside-route",
                    start=date(2026, 10, 5),
                    end=date(2026, 10, 5),
                    page=1,
                )
    assert gateway_wire["sdk_calls"] == []


@pytest.mark.parametrize("size,total", [(0, 1), (-1, 1), (1000, -1)])
def test_invalid_page_limits_are_contract_errors(size, total):
    reader, _ = _reader(
        {
            "list": [],
            "page_info": {
                "page": 1,
                "page_size": size,
                "total_page": 0,
                "total_number": total,
            },
        }
    )
    with pytest.raises(DomainError):
        reader.account_day(
            advertiser_ids=("a",),
            start=date(2026, 10, 5),
            end=date(2026, 10, 5),
            page=1,
        )


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
def test_report_gateway_bc_read_uses_frozen_bc(
    database_engine, redis_client, gateway_case, gateway_wire
):
    gateway_wire["sdk_data"]["data"] = PAGE
    with gateway(database_engine, redis_client, gateway_case) as client:
        assert (
            client.report_reader.bc_spend(
                bc_id=gateway_case[1].bc_id,
                start=date(2026, 10, 5),
                end=date(2026, 10, 5),
                page=1,
            ).rows
            == ()
        )
    assert len(gateway_wire["sdk_calls"]) == 1


def test_report_parameter_error_is_terminal_and_timeout_is_distinct():
    from contextlib import nullcontext
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from business_api_client import ApiClient
    from urllib3.exceptions import ReadTimeoutError

    from app.integrations.tiktok.official.accounts import OfficialReadRequests

    client = ApiClient()
    requests = OfficialReadRequests(
        client,
        request_scope=lambda *_: nullcontext(),
        deadline=datetime.now(UTC) + timedelta(seconds=20),
    )

    def invalid(_timeout):
        return client.deserialize(
            SimpleNamespace(data=b'{"code":40002,"message":"invalid","data":{}}'),
            "InlineResponse200",
        )

    with pytest.raises(DomainError) as caught:
        requests.invoke("reports.integrated", "a", invalid, exact_numbers=True)
    assert caught.value.code == "report_query_invalid"

    def timeout(_timeout):
        raise ReadTimeoutError(None, "synthetic", "timeout")

    with pytest.raises(DomainError) as caught:
        requests.invoke("reports.integrated", "a", timeout, exact_numbers=True)
    assert caught.value.code == "report_timeout"


def test_campaign_discovery_uses_local_days_and_all_statuses():
    reader, rec = _reader(PAGE)
    reader.campaign_day(
        advertiser_ids=("a",), start=date(2026, 10, 5), end=date(2026, 10, 5), page=1
    )
    args = rec.calls[0][2]
    assert args["dimensions"] == ["campaign_id", "stat_time_day"]
    assert "multi_adv_report_in_utc_time" not in args
    assert args["filtering"][0]["field_name"] == "campaign_status"


def test_bc_permission_error_keeps_safe_classification():
    from contextlib import nullcontext
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from business_api_client import ApiClient

    from app.integrations.tiktok.official.accounts import OfficialReadRequests

    client = ApiClient()
    requests = OfficialReadRequests(
        client,
        request_scope=lambda *_: nullcontext(),
        deadline=datetime.now(UTC) + timedelta(seconds=20),
    )

    def send(_timeout):
        return client.deserialize(
            SimpleNamespace(
                data=b'{"code":40001,"message":"No permission to read this BC report","data":{}}'
            ),
            "InlineResponse200",
        )

    with pytest.raises(DomainError) as caught:
        requests.invoke("reports.bc", None, send, exact_numbers=True)
    assert caught.value.code == "report_permission_denied"


def test_physical_counter_excludes_local_admission_and_validation(monkeypatch):
    from contextlib import contextmanager
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from business_api_client import ApiClient, ReportingApi

    from app.integrations.tiktok.gateway import _sdk_report_send
    from app.integrations.tiktok.sdk import AccountAdmissionDeferred
    from app.modules.reporting.jobs import _CountingReader

    client = ApiClient()
    client.default_headers["Access-Token"] = "synthetic-only"
    blocked = [True]

    @contextmanager
    def scope(*_args):
        if blocked[0]:
            raise AccountAdmissionDeferred(300000)
        yield

    def throttle(*_args, **_kwargs):
        return client.deserialize(
            SimpleNamespace(data=b'{"code":40100,"message":"QPM limit","data":{}}'),
            "InlineResponse200",
        )

    monkeypatch.setattr(ReportingApi, "report_integrated_get", throttle)
    reader = _CountingReader(
        SdkReportReader(
            send=_sdk_report_send(
                client, scope, datetime.now(UTC) + timedelta(seconds=60)
            )
        )
    )
    args = {
        "advertiser_ids": ("a",),
        "start": date(2026, 10, 5),
        "end": date(2026, 10, 5),
        "page": 1,
    }
    with pytest.raises(AccountAdmissionDeferred):
        reader.ad_day(**args)
    assert reader.count == 0
    with pytest.raises(DomainError):
        reader.ad_day(**(args | {"advertiser_ids": ()}))
    assert reader.count == 0
    blocked[0] = False
    with pytest.raises(AccountAdmissionDeferred):
        reader.ad_day(**args)
    assert reader.count == 1


def test_batch_permission_check_runs_once_after_admission(monkeypatch):
    """额度拒绝不遍历整批账户；准入后仍在物理发送前核验全体权限。"""
    from contextlib import contextmanager
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from business_api_client import ApiClient, ReportingApi

    from app.integrations.tiktok.gateway import _sdk_report_send
    from app.integrations.tiktok.sdk import AccountAdmissionDeferred

    client = ApiClient()
    client.default_headers["Access-Token"] = "synthetic-only"
    events = []
    blocked = [True]

    @contextmanager
    def scope(*_args):
        events.append("admission")
        if blocked[0]:
            raise AccountAdmissionDeferred(1000)
        yield

    def check(*_args):
        events.append("permissions")

    def send(*_args, **_kwargs):
        events.append("http")
        return client.deserialize(
            SimpleNamespace(data=b'{"code":0,"data":{}}'), "InlineResponse200"
        )

    monkeypatch.setattr(ReportingApi, "report_integrated_get", send)
    reader = SdkReportReader(
        send=_sdk_report_send(
            client,
            scope,
            datetime.now(UTC) + timedelta(seconds=60),
            request_check=check,
        )
    )
    args = {
        "advertiser_ids": ("a", "b"),
        "start": date(2026, 10, 5),
        "end": date(2026, 10, 5),
        "page": 1,
    }
    with pytest.raises(AccountAdmissionDeferred):
        reader.ad_day(**args)
    assert events == ["admission"]
    events.clear()
    blocked[0] = False
    reader.ad_day(**args)
    assert events == ["admission", "permissions", "http"]


def test_material_overview_filters_exact_advertiser_ad_ids():
    reader, rec = _reader(PAGE)
    reader.material_ad_links(advertiser_id="a", page=1, ad_ids=("ad-1", "ad-2"))
    assert rec.calls[0][2]["filtering"] == {"smart_plus_ad_ids": ["ad-1", "ad-2"]}


@pytest.mark.parametrize("ids", [("ad-1", "ad-1"), tuple(map(str, range(201)))])
def test_material_overview_rejects_invalid_ad_filter_before_send(ids):
    reader, rec = _reader(PAGE)
    with pytest.raises(DomainError, match="筛选"):
        reader.material_ad_links(advertiser_id="a", page=1, ad_ids=ids)
    assert rec.calls == []


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
@pytest.mark.parametrize("workload", ["live", "background"])
def test_report_service_class_reaches_each_physical_sdk_call(
    database_engine, redis_client, gateway_case, gateway_wire, workload
):
    from datetime import UTC, datetime, timedelta

    from app.core.config import settings
    from app.integrations.tiktok.gateway import open_tiktok_gateway
    from app.jobs.admission import admission_keys

    context, route, advertiser = gateway_case
    gateway_wire["sdk_data"]["data"] = PAGE
    with open_tiktok_gateway(
        database_engine=database_engine,
        redis_client=redis_client,
        context=context,
        route=route,
        workload=workload,
        task_deadline=datetime.now(UTC) + timedelta(seconds=20),
    ) as client:
        client.report_reader.ad_day(
            advertiser_ids=(advertiser,),
            start=date(2026, 10, 5),
            end=date(2026, 10, 5),
            page=1,
        )
    keys = admission_keys(
        settings.TIKTOK_APP_ID,
        "reports.integrated",
        context.tenant_id,
        advertiser,
        "GET /open_api/v1.3/report/integrated/get/",
    )
    assert redis_client.zcard(keys[0] + ":" + workload) == 1
    assert redis_client.zcard(keys[1] + ":" + workload) == 1
    assert redis_client.zcard(keys[2] + ":" + workload) == 0
    assert len(gateway_wire["sdk_calls"]) == 1


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
def test_physical_report_aliases_share_quota_other_endpoints_remain_available(
    database_engine, redis_client, gateway_case, gateway_wire, monkeypatch
):
    from datetime import UTC, datetime, timedelta

    from app.core.config import settings
    from app.integrations.tiktok.gateway import open_tiktok_gateway
    from app.integrations.tiktok.sdk import AccountAdmissionDeferred

    context, route, advertiser = gateway_case
    config = settings.TIKTOK_CALL_POLICIES
    monkeypatch.setattr(
        settings,
        "TIKTOK_CALL_POLICIES",
        config
        | {
            "base": config["base"]
            | {
                "app_calls_per_window": 1,
                "endpoint_calls_per_window": 1,
                "window_ms": 10000,
            }
        },
    )
    gateway_wire["sdk_data"]["data"] = PAGE
    with open_tiktok_gateway(
        database_engine=database_engine,
        redis_client=redis_client,
        context=context,
        route=route,
        task_deadline=datetime.now(UTC) + timedelta(seconds=20),
    ) as client:
        reader = client.report_reader
        reader.ad_day(
            advertiser_ids=(advertiser,),
            start=date(2026, 10, 5),
            end=date(2026, 10, 5),
            page=1,
        )
        with pytest.raises(AccountAdmissionDeferred) as caught:
            reader.bc_spend(
                bc_id=route.bc_id,
                start=date(2026, 10, 5),
                end=date(2026, 10, 5),
                page=1,
            )
        assert caught.value.admission_source == "local"
        reader.material_ad_links(advertiser_id=advertiser, page=1)
    assert len(gateway_wire["sdk_calls"]) == 2


def test_api_read_policy_override_cannot_raise_mcp_policy(monkeypatch):
    from app.core.config import settings
    from app.jobs.admission import admission_policy

    base = dict(  # noqa: C408
        app_max_inflight=12,
        endpoint_max_inflight=3,
        tenant_max_inflight=10,
        advertiser_max_inflight=4,
        app_calls_per_window=10,
        endpoint_calls_per_window=3,
        window_ms=1000,
        lease_ms=960000,
    )
    monkeypatch.setattr(
        settings,
        "TIKTOK_CALL_POLICIES",
        {
            "base": base,
            "endpoints": {
                "OFFICIAL_API:reports.integrated": {
                    "endpoint_calls_per_window": 10,
                    "endpoint_max_inflight": 6,
                }
            },
        },
    )
    assert (
        admission_policy(
            "reports.integrated", channel="OFFICIAL_API"
        ).endpoint_calls_per_window
        == 10
    )
    assert (
        admission_policy(
            "reports.integrated", channel="OFFICIAL_MCP"
        ).endpoint_calls_per_window
        == 3
    )


def test_non_spark_asset_identity_reads_exact_material_ids():
    body = {
        "list": [{"material_id": "mid", "video_id": "vid"}],
        "page_info": {"page": 1, "page_size": 100, "total_page": 1, "total_number": 1},
    }
    reader, rec = _reader(body)
    result = reader.material_videos(advertiser_id="a", material_ids=("mid",))
    assert result.rows == ({"material_id": "mid", "video_id": "vid"},)
    assert rec.calls == [
        (
            "materials.search_videos",
            "a",
            {
                "advertiser_id": "a",
                "filtering": {"material_ids": ["mid"]},
                "page": 1,
                "page_size": 100,
            },
        )
    ]


@pytest.mark.parametrize(
    "rows",
    [
        [{"material_id": "other", "video_id": "vid"}],
        [{"material_id": "mid", "video_id": "vid", "advertiser_id": "other-account"}],
        [{"material_id": "mid", "video_id": ""}],
        [
            {"material_id": "mid", "video_id": "v1"},
            {"material_id": "mid", "video_id": "v2"},
        ],
    ],
)
def test_asset_identity_rejects_foreign_or_ambiguous_pairs(rows):
    body = {
        "list": rows,
        "page_info": {
            "page": 1,
            "page_size": 100,
            "total_page": 1,
            "total_number": len(rows),
        },
    }
    reader, _ = _reader(body)
    with pytest.raises(DomainError):
        reader.material_videos(advertiser_id="a", material_ids=("mid",))


def test_asset_identity_uses_official_sdk_scoped_physical_request(monkeypatch):
    from contextlib import contextmanager
    from datetime import UTC, datetime, timedelta

    from urllib3.response import HTTPResponse

    from app.integrations.tiktok.gateway import _sdk_report_send
    from app.integrations.tiktok.sdk import official_client

    calls, admissions = [], []

    def wire(_pool, method, url, **kwargs):
        calls.append((method, url, dict(kwargs["fields"])))
        return HTTPResponse(
            body=json.dumps(
                {
                    "code": 0,
                    "data": {
                        "list": [{"material_id": "mid", "video_id": "vid"}],
                        "page_info": {
                            "page": 1,
                            "page_size": 100,
                            "total_page": 1,
                            "total_number": 1,
                        },
                    },
                }
            ).encode(),
            status=200,
        )

    @contextmanager
    def scope(adv, op, _deadline):
        admissions.append((adv, op))
        yield

    monkeypatch.setattr("urllib3.PoolManager.request", wire)
    with official_client(access_token="synthetic-test") as client:
        reader = SdkReportReader(
            send=_sdk_report_send(
                client, scope, datetime.now(UTC) + timedelta(seconds=30)
            )
        )
        assert (
            reader.material_videos(advertiser_id="a", material_ids=("mid",)).rows[0][
                "video_id"
            ]
            == "vid"
        )
        assert reader.request_count == 1
    assert admissions == [("a", "materials.search_videos")]
    method, url, fields = calls[0]
    assert method == "GET" and url.endswith("/file/video/ad/search/")
    assert fields["advertiser_id"] == "a"
    assert json.loads(fields["filtering"]) == {"material_ids": ["mid"]}
