"""目录身份、RF1 命名和报表边界不能靠隐式转换修补。"""

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from app.integrations.tiktok.contracts.ads import (
    AdEntity,
    AdMaterialUsage,
    DirectoryPage,
    DirectoryQuery,
    EntityRef,
    MaterialUseRef,
)
from app.integrations.tiktok.contracts.common import CallEvidence
from app.integrations.tiktok.contracts.reporting import (
    ReportPage,
    ReportQuery,
    ReportRow,
    ReportTask,
)
from app.modules.ads.naming import CampaignIdentity, parse_campaign_name

TENANT = UUID("00000000-0000-0000-0000-000000000001")
OTHER_TENANT = UUID("00000000-0000-0000-0000-000000000002")
NOW = datetime(2026, 9, 30, tzinfo=UTC)
EVIDENCE = CallEvidence(request_id="request-1")


def entity_ref(kind="ad", **changes):
    return EntityRef(
        **{
            "tenant_id": TENANT,
            "advertiser_id": "00123",
            "kind": kind,
            "remote_id": "90071992547409939999",
            **changes,
        }
    )


def entity(**changes):
    return AdEntity(
        **{
            "ref": entity_ref(),
            "parent_ref": entity_ref("adgroup"),
            "ad_type": "REGULAR",
            "name": "原始 名称",
            "configuration": {},
            "operation_status": None,
            "review_status": None,
            "delivery_status": None,
            "observed_at": NOW,
            **changes,
        }
    )


def directory_query(**changes):
    return DirectoryQuery(
        **{
            "advertiser_id": "00123",
            "kind": "ad",
            "ad_type": "REGULAR",
            "page": 1,
            "page_size": 50,
            "ids": (),
            "parent_ids": (),
            "include_deleted": False,
            **changes,
        }
    )


def report_query(**changes):
    return ReportQuery(
        **{
            "advertiser_id": "00123",
            "report_contract": "BASIC_AD",
            "metric_family": "delivery",
            "dimensions": ("ad_id",),
            "metrics": ("spend", "revenue"),
            "start_date": date(2026, 9, 29),
            "end_date": date(2026, 9, 30),
            "granularity": "DAY",
            "currency": "USD",
            "timezone": "Asia/Shanghai",
            "attribution": "7d_click_1d_view",
            "filter_ids": (),
            "page": 1,
            **changes,
        }
    )


def report_row(**changes):
    return ReportRow(
        **{
            "subject_key": ("00123", "ad-1"),
            "bucket_start": NOW,
            "bucket_end": NOW + timedelta(days=1),
            "values": {"spend": Decimal("0"), "revenue": None},
            "availability": {"spend": "AVAILABLE", "revenue": "UNAVAILABLE"},
            **changes,
        }
    )


@pytest.mark.parametrize(
    "name,provider,drama",
    [
        (" 嘉书 - 总裁归来 -账户2", "嘉书", "总裁归来"),
        ("嘉书-总裁归来", "嘉书", "总裁归来"),
        ("嘉书-总裁归来-", "嘉书", "总裁归来"),
        ("嘉书-Cafe\u0301-备注", "嘉书", "Café"),
        ("网眼-My  Drama-备注-更多备注", "网眼", "My  Drama"),
    ],
)
def test_rf1_parses_only_first_two_ascii_hyphen_segments(name, provider, drama):
    assert parse_campaign_name(name) == CampaignIdentity(provider, drama, "VALID")


@pytest.mark.parametrize(
    "name", ["嘉书--测试", "嘉书- -测试", "-剧名", "剧名", "", "嘉书－剧名"]
)
def test_rf1_empty_segments_and_non_ascii_separator_do_not_guess_identity(name):
    result = parse_campaign_name(name)
    assert result.status == "INVALID"
    assert result.drama_name is None


def test_rf1_unicode_and_remarks_do_not_change_group_but_case_and_provider_do():
    assert parse_campaign_name("嘉书-Cafe\u0301-备注") == parse_campaign_name(
        "嘉书-Café"
    )
    assert parse_campaign_name("嘉书-Café") != parse_campaign_name("网眼-Café")
    assert parse_campaign_name("嘉书-Café") != parse_campaign_name("嘉书-café")


@pytest.mark.parametrize(
    "values",
    [(None, "剧", "VALID"), ("版权方", " ", "VALID"), ("版权方", "剧", "UNKNOWN")],
)
def test_identity_rejects_inconsistent_valid_status(values):
    with pytest.raises(ValueError):
        CampaignIdentity(*values)


@pytest.mark.parametrize(
    "changes",
    [
        {"tenant_id": str(TENANT)},
        {"advertiser_id": " "},
        {"advertiser_id": 123},
        {"remote_id": ""},
        {"remote_id": 123},
        {"kind": "material"},
    ],
)
def test_entity_ref_rejects_invalid_scope_and_ids(changes):
    with pytest.raises(ValueError):
        entity_ref(**changes)


def test_exact_identity_keeps_leading_zeros_and_is_frozen():
    ref = entity_ref()
    assert ref.advertiser_id == "00123"
    assert ref.remote_id == "90071992547409939999"
    with pytest.raises(FrozenInstanceError):
        ref.remote_id = "different"
    assert replace(ref, advertiser_id="other") != ref
    assert replace(ref, tenant_id=OTHER_TENANT) != ref


@pytest.mark.parametrize(
    "changes",
    [
        {"parent_ref": entity_ref("adgroup", tenant_id=OTHER_TENANT)},
        {"parent_ref": entity_ref("adgroup", advertiser_id="other")},
        {"ref": "ad-1"},
        {"parent_ref": "parent-1"},
        {"ad_type": ""},
        {"name": None},
        {"configuration": []},
        {"operation_status": True},
        {"observed_at": NOW.replace(tzinfo=None)},
        {"observed_at": date(2026, 9, 30)},
    ],
)
def test_ad_entity_rejects_cross_scope_parent_and_malformed_details(changes):
    with pytest.raises(ValueError):
        entity(**changes)


def test_ad_entity_allows_missing_parent_and_defensively_copies_configuration():
    config = {"targeting": {"locations": ["region-1"]}}
    result = entity(parent_ref=None, configuration=config)
    config["targeting"]["locations"].append("region-2")
    assert result.parent_ref is None
    assert result.configuration == {"targeting": {"locations": ["region-1"]}}
    assert type(result.configuration) is dict


def test_ordinary_material_keeps_missing_ad_material_id_without_vid_substitution():
    use = MaterialUseRef(entity_ref(), "video-1", None, "VIDEO")
    usage = AdMaterialUsage(use, None, None, True)
    assert usage.use_ref.ad_material_id is None
    assert usage.use_ref.platform_material_id == "video-1"
    assert (
        replace(use, ad_material_id="ad-material-1").ad_material_id == "ad-material-1"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"ad_ref": entity_ref("campaign")},
        {"platform_material_id": ""},
        {"ad_material_id": " "},
        {"material_type": ""},
    ],
)
def test_material_use_rejects_invalid_identity(changes):
    with pytest.raises(ValueError):
        MaterialUseRef(
            **{
                "ad_ref": entity_ref(),
                "platform_material_id": "video-1",
                "ad_material_id": None,
                "material_type": "VIDEO",
                **changes,
            }
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"use_ref": "video-1"},
        {"local_material_id": "local-1"},
        {"operation_status": 1},
        {"complete": 1},
    ],
)
def test_material_usage_rejects_untyped_facts(changes):
    with pytest.raises(ValueError):
        AdMaterialUsage(
            **{
                "use_ref": MaterialUseRef(entity_ref(), "video-1", None, "VIDEO"),
                "local_material_id": None,
                "operation_status": None,
                "complete": True,
                **changes,
            }
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"advertiser_id": ""},
        {"kind": "material"},
        {"ad_type": ""},
        {"page": 0},
        {"page": True},
        {"page_size": 0},
        {"page_size": False},
        {"ids": ["ad-1"]},
        {"ids": ("",)},
        {"parent_ids": (" ",)},
        {"include_deleted": 1},
    ],
)
def test_directory_query_rejects_invalid_identifiers_and_pagination(changes):
    with pytest.raises(ValueError):
        directory_query(**changes)


@pytest.mark.parametrize(
    "page_type,items_field", [(DirectoryPage, "items"), (ReportPage, "rows")]
)
@pytest.mark.parametrize(
    "changes",
    [
        {"complete": True, "next_page": 2},
        {"next_page": 0},
        {"next_page": True},
        {"complete": 1},
        {"evidence": {}},
    ],
)
def test_pages_reject_completion_conflicts_and_untyped_evidence(
    page_type, items_field, changes
):
    payload = {
        items_field: (),
        "next_page": None,
        "complete": True,
        "evidence": EVIDENCE,
    }
    if page_type is DirectoryPage:
        payload["materials"] = ()
    with pytest.raises(ValueError):
        page_type(**{**payload, **changes})


def test_incomplete_page_without_continuation_is_not_promoted_to_complete():
    page = ReportPage((), None, False, EVIDENCE)
    assert page.complete is False
    assert page.evidence is EVIDENCE
    assert DirectoryPage((), (), 2, False, EVIDENCE).next_page == 2
    assert DirectoryPage((entity(),), (), None, True, EVIDENCE).complete is True


@pytest.mark.parametrize(
    "changes",
    [{"items": [entity()]}, {"items": ("ad-1",)}, {"materials": ("video-1",)}],
)
def test_directory_page_rejects_untyped_contents(changes):
    with pytest.raises(ValueError):
        DirectoryPage(
            **{
                "items": (),
                "materials": (),
                "next_page": None,
                "complete": True,
                "evidence": EVIDENCE,
                **changes,
            }
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"advertiser_id": " "},
        {"report_contract": ""},
        {"metric_family": ""},
        {"dimensions": ["ad_id"]},
        {"dimensions": ("",)},
        {"metrics": ()},
        {"metrics": ("spend", "spend")},
        {"start_date": NOW},
        {"end_date": "2026-09-30"},
        {"start_date": date(2026, 10, 1)},
        {"granularity": ""},
        {"currency": ""},
        {"timezone": ""},
        {"attribution": ""},
        {"filter_ids": ("",)},
        {"page": 0},
        {"page": True},
    ],
)
def test_report_query_rejects_invalid_dates_metadata_and_identifiers(changes):
    with pytest.raises(ValueError):
        report_query(**changes)


def test_report_query_accepts_same_day_and_exact_metadata():
    query = report_query(start_date=date(2026, 9, 30))
    assert query.start_date == query.end_date
    assert query.timezone == "Asia/Shanghai"
    with pytest.raises(FrozenInstanceError):
        query.page = 2


@pytest.mark.parametrize(
    "changes",
    [
        {"subject_key": ()},
        {"subject_key": ("",)},
        {"subject_key": ["ad-1"]},
        {"bucket_start": NOW.replace(tzinfo=None)},
        {"bucket_end": NOW.replace(tzinfo=None)},
        {"bucket_end": NOW},
        {"bucket_end": NOW - timedelta(hours=1)},
        {"values": {"spend": 0.1}},
        {"values": {"spend": Decimal("NaN")}},
        {"values": {"spend": Decimal("Infinity")}},
        {"values": {"": None}},
        {"availability": {"spend": None}},
        {"availability": {"spend": ""}},
    ],
)
def test_report_row_rejects_ambiguous_buckets_and_non_decimal_values(changes):
    with pytest.raises(ValueError):
        report_row(**changes)


def test_report_row_keeps_missing_values_distinct_from_exact_decimal_zero():
    values = {"spend": Decimal("0"), "revenue": None}
    availability = {"spend": "AVAILABLE", "revenue": "UNAVAILABLE"}
    row = report_row(values=values, availability=availability)
    values["revenue"] = Decimal("9")
    availability["revenue"] = "AVAILABLE"
    assert row.values == {"spend": Decimal("0"), "revenue": None}
    assert row.availability["revenue"] == "UNAVAILABLE"
    assert ReportPage((row,), None, True, EVIDENCE).rows == (row,)


@pytest.mark.parametrize("rows", [[], ("unparsed row",)])
def test_report_page_rejects_unparsed_or_mutable_rows(rows):
    with pytest.raises(ValueError):
        ReportPage(rows, None, True, EVIDENCE)


@pytest.mark.parametrize(
    "changes",
    [
        {"task_id": ""},
        {"advertiser_id": "other"},
        {"status": "DONE"},
        {"query": {}},
    ],
)
def test_report_task_rejects_invalid_or_cross_account_identity(changes):
    with pytest.raises(ValueError):
        ReportTask(
            **{
                "task_id": "task-1",
                "advertiser_id": "00123",
                "status": "PENDING",
                "query": report_query(),
                **changes,
            }
        )


@pytest.mark.parametrize("status", ["PENDING", "RUNNING", "READY", "FAILED"])
def test_report_task_keeps_query_and_known_status(status):
    query = report_query()
    task = ReportTask("task-1", "00123", status, query)
    assert task.query == query
    assert task.status == status


@pytest.mark.parametrize(
    "page_type,items_field", [(DirectoryPage, "items"), (ReportPage, "rows")]
)
@pytest.mark.parametrize("page,next_page", [(0, None), (True, None), (3, 3), (3, 2)])
def test_pages_reject_invalid_current_page_and_non_advancing_continuation(
    page_type, items_field, page, next_page
):
    payload = {
        items_field: (),
        "page": page,
        "next_page": next_page,
        "complete": False,
        "evidence": EVIDENCE,
    }
    if page_type is DirectoryPage:
        payload["materials"] = ()
    with pytest.raises(ValueError):
        page_type(**payload)


def test_pages_keep_actual_page_number_for_resume_and_duplicate_detection():
    directory = DirectoryPage((), (), 4, False, EVIDENCE, page=3)
    report = ReportPage((), None, True, EVIDENCE, page=3)
    assert directory.page == 3
    assert report.page == 3
    assert ReportPage((), None, True, EVIDENCE).page == 1


def test_report_subject_can_repeat_identifiers_at_distinct_positions():
    assert report_row(subject_key=("123", "123")).subject_key == ("123", "123")


def test_report_bucket_compares_instants_across_dst_fold():
    from zoneinfo import ZoneInfo

    timezone = ZoneInfo("America/New_York")
    start = datetime(2026, 11, 1, 1, 30, tzinfo=timezone, fold=0)
    end = datetime(2026, 11, 1, 1, 15, tzinfo=timezone, fold=1)
    assert report_row(bucket_start=start, bucket_end=end).bucket_end == end
    with pytest.raises(ValueError):
        report_row(bucket_start=end, bucket_end=start)
