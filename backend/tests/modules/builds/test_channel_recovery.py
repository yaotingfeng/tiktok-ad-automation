"""双通道核查只确认完整且唯一的原对象，从不把空页作为重建依据。"""

from decimal import Decimal

import pytest

from app.integrations.tiktok.contracts.builds import (
    BuildPage,
    BuildRecord,
    CampaignCreate,
)
from app.modules.builds import reconciliation
from tests.integrations.tiktok.test_build_adapters import build_case as build_case


@pytest.mark.parametrize(
    "complete,count,missing,expected",
    [
        (False, 0, (), "UNKNOWN"),
        (True, 0, (), "UNKNOWN"),
        (False, 2, (), "UNKNOWN"),
        (True, 2, (), "UNKNOWN"),
        (True, 1, ("budget",), "UNKNOWN"),
        (True, 1, (), "CONFIRMED"),
    ],
)
def test_only_complete_unique_observation_confirms(complete, count, missing, expected):
    intent = CampaignCreate(
        advertiser_id="synthetic-ad", name="Exact", budget=Decimal("1")
    )
    matches = tuple(
        BuildRecord(
            remote_id=f"remote-{n}",
            intent=None if missing else intent,
            operation_status="ENABLE",
            missing_fields=missing,
        )
        for n in range(count)
    )
    # 纯判定刻意不要求传输证据；实际 adapter/worker 用真实校验的 envelope。
    page = BuildPage.model_construct(
        rows=matches,
        page=1,
        total_pages=1,
        total_number=count,
        complete=complete,
        evidence=None,
    )
    decision = getattr(reconciliation, "reconciliation_decision", None)
    assert callable(decision), "业务层需明确区分已确认对象与仍未知结果"
    assert decision(page=page, matches=matches) == expected


def test_missing_adgroup_status_preserves_only_fully_observed_typed_fields():
    from app.integrations.tiktok.contracts.builds import BuildReadQuery
    from app.integrations.tiktok.contracts.common import (
        CallEvidence,
        McpBusinessResponse,
    )
    from app.modules.builds.readback_compare import parse_page
    from app.modules.builds.request_compiler import decode_intent
    from tests.contracts.test_tiktok_build_contract import build_bodies

    body = build_bodies()["ADGROUP"]
    actual = {**body, "adgroup_id": "remote-group"}
    del actual["operation_status"]
    query = BuildReadQuery(intent=decode_intent("ADGROUP", body))
    response = McpBusinessResponse(
        data={
            "list": [actual],
            "page_info": {
                "page": 1,
                "page_size": 100,
                "total_page": 1,
                "total_number": 1,
            },
        },
        evidence=CallEvidence(request_id="synthetic-observation"),
    )
    record = parse_page(query=query, response=response).rows[0]
    assert record.intent is None and record.missing_fields == ("operation_status",)
    observed = getattr(record, "observed_adgroup", None)
    assert observed is not None, "独立状态查询之前必须保留其余实际字段的类型证据"
    assert observed.campaign_id == "90071992547409939998"
    assert observed.roas_bid == Decimal("1.234567890123456789")
    assert "operation_status" not in observed.model_dump()


@pytest.mark.parametrize("change", ["missing_parent", "inexact_money"])
def test_partial_or_inexact_group_fields_cannot_be_repaired_by_status(change):
    from app.integrations.tiktok.contracts.builds import BuildReadQuery
    from app.integrations.tiktok.contracts.common import (
        CallEvidence,
        McpBusinessResponse,
    )
    from app.modules.builds.readback_compare import parse_page
    from app.modules.builds.request_compiler import decode_intent
    from tests.contracts.test_tiktok_build_contract import build_bodies

    body = build_bodies()["ADGROUP"]
    actual = {**body, "adgroup_id": "remote-group"}
    del actual["operation_status"]
    if change == "missing_parent":
        del actual["campaign_id"]
    else:
        actual["roas_bid"] = 1.234567890123456789
    record = parse_page(
        query=BuildReadQuery(intent=decode_intent("ADGROUP", body)),
        response=McpBusinessResponse(
            data={
                "list": [actual],
                "page_info": {
                    "page": 1,
                    "page_size": 100,
                    "total_page": 1,
                    "total_number": 1,
                },
            },
            evidence=CallEvidence(),
        ),
    ).rows[0]
    assert record.intent is None
    assert getattr(record, "observed_adgroup", None) is None


@pytest.mark.parametrize(
    "status,changed_id", [("ENABLE", False), ("DISABLE", False), ("ENABLE", True)]
)
def test_status_combination_preserves_actual_core_without_inventing_enable(
    status, changed_id
):
    from app.integrations.tiktok.contracts.builds import (
        AdGroupObservedFacts,
        AdGroupStatus,
    )
    from app.integrations.tiktok.contracts.common import CallEvidence, RemoteCallError
    from app.modules.builds.readback_compare import with_adgroup_status
    from app.modules.builds.request_compiler import decode_intent
    from tests.contracts.test_tiktok_build_contract import build_bodies

    intent = decode_intent("ADGROUP", build_bodies()["ADGROUP"])
    facts = AdGroupObservedFacts.model_validate(
        intent.model_dump(exclude={"operation_status"})
    )
    record = BuildRecord(
        remote_id="remote-group",
        intent=None,
        operation_status=None,
        missing_fields=("operation_status",),
        observed_adgroup=facts,
    )
    actual = AdGroupStatus(
        advertiser_id=facts.advertiser_id,
        adgroup_id="foreign-group" if changed_id else "remote-group",
        operation_status=status,
        evidence=CallEvidence(),
    )
    if changed_id:
        with pytest.raises(RemoteCallError):
            with_adgroup_status(record=record, status=actual)
        return
    result = with_adgroup_status(record=record, status=actual)
    assert result.operation_status == status
    assert result.observed_adgroup.campaign_id == "90071992547409939998"
    if status == "ENABLE":
        assert result.intent is not None and not result.missing_fields
        assert result.intent.roas_bid == Decimal("1.234567890123456789")
    else:
        assert result.intent is None and result.missing_fields == ("operation_status",)


@pytest.mark.parametrize(
    "remote_id",
    ["https://signed.example.test/object?signature=private", "remote\ncontrol"],
)
def test_http_readback_rejects_url_and_control_character_object_ids(
    build_case, remote_id
):
    from app.integrations.tiktok.contracts.builds import BuildReadQuery
    from app.integrations.tiktok.contracts.common import RemoteCallError
    from app.modules.builds.request_compiler import decode_intent
    from tests.contracts.test_tiktok_build_contract import build_bodies

    adapter, wire, _ = build_case
    body = build_bodies()["CAMPAIGN"]
    wire.enqueue_readback(
        "CAMPAIGN", [{**body, "campaign_id": remote_id}], page=1, total=1
    )
    with pytest.raises(RemoteCallError):
        adapter.read_page(query=BuildReadQuery(intent=decode_intent("CAMPAIGN", body)))
    assert len(wire.business_calls()) == 1


def test_remote_id_validation_keeps_actual_chinese_name_valid(build_case):
    from app.integrations.tiktok.contracts.builds import BuildReadQuery
    from app.modules.builds.readback_compare import compare_record
    from app.modules.builds.request_compiler import decode_intent
    from tests.contracts.test_tiktok_build_contract import build_bodies

    adapter, wire, _ = build_case
    body = {**build_bodies()["CAMPAIGN"], "campaign_name": "真实中文名称"}
    wire.enqueue_readback(
        "CAMPAIGN", [{**body, "campaign_id": "safe:remote-123"}], page=1, total=1
    )
    query = BuildReadQuery(intent=decode_intent("CAMPAIGN", body))
    assert (
        compare_record(query=query, record=adapter.read_page(query=query).rows[0])
        == "MATCH"
    )
