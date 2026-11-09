from datetime import UTC, datetime

import pytest

from app.core.errors import DomainError
from tests.integrations.tiktok.gateway_support import (  # noqa: F401
    database_engine as database_engine,
)
from tests.integrations.tiktok.gateway_support import (
    gateway,
)
from tests.integrations.tiktok.gateway_support import (
    gateway_case as gateway_case,
)
from tests.integrations.tiktok.gateway_support import (
    gateway_wire as gateway_wire,
)
from tests.modules.accounts.conftest import app_config, policy  # noqa: F401


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
def test_official_sdk_all_three_endpoints_and_authorization(
    database_engine, redis_client, gateway_case, gateway_wire
):
    context, route, advertiser = gateway_case
    with gateway(database_engine, redis_client, gateway_case) as opened:
        reader = opened.report_reader
        gateway_wire["sdk_data"]["data"] = {"task_id": "export-test"}
        assert (
            reader.change_log_create(
                advertiser_id=advertiser,
                start=datetime(2026, 11, 1, tzinfo=UTC),
                end=datetime(2026, 11, 2, tzinfo=UTC),
            )
            == "export-test"
        )
        gateway_wire["sdk_data"]["data"] = {"status": "SUCCESS"}
        assert (
            reader.change_log_check(advertiser_id=advertiser, task_id="export-test")
            == "SUCCESS"
        )
        gateway_wire["sdk_data"]["data"] = {"status": "SUCCESS", "changelog": "csv"}
        assert (
            reader.change_log_download(advertiser_id=advertiser, task_id="export-test")
            == "csv"
        )
        assert reader.request_count == 3
        before = len(gateway_wire["sdk_calls"])
        with pytest.raises(DomainError):
            reader.change_log_check(
                advertiser_id="not-authorized", task_id="export-test"
            )
        assert len(gateway_wire["sdk_calls"]) == before
    assert [
        (method, url.split("/v1.3/")[1]) for method, url, _ in gateway_wire["sdk_calls"]
    ] == [
        ("POST", "changelog/task/create/"),
        ("GET", "changelog/task/check/"),
        ("GET", "changelog/task/download/"),
    ]
