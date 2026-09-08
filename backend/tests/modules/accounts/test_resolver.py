from app.modules.accounts.resolver import parse_matching_rows, resolve_lines
from app.modules.accounts.schemas import InputLine
from tests.modules.accounts.test_access import (
    account_access_case as account_access_case,
)


def test_ids_names_ambiguity_and_original_lines():
    rows = [
        {"advertiser_id": "90071992547409931", "name": "完整账户A"},
        {"advertiser_id": "2", "name": "重名"},
        {"advertiser_id": "3", "name": "重名"},
    ]
    results = parse_matching_rows(
        [
            InputLine(line_no=1, raw="90071992547409931"),
            InputLine(line_no=2, raw=" 完整账户A "),
            InputLine(line_no=3, raw="重名"),
            InputLine(line_no=4, raw="不存在"),
            InputLine(line_no=5, raw="  "),
        ],
        rows,
    )
    assert [row.status for row in results] == [
        "MATCHED",
        "DUPLICATE",
        "AMBIGUOUS",
        "NOT_FOUND",
        "EMPTY",
    ]
    assert results[1].duplicate_of == 1 and results[1].raw == " 完整账户A "
    assert results[2].candidates == ["2", "3"]
    assert results[3].raw == "不存在"


def test_exact_id_wins_over_same_text_name():
    result = parse_matching_rows(
        [InputLine(line_no=8, raw="123")],
        [
            {"advertiser_id": "123", "name": "A"},
            {"advertiser_id": "456", "name": "123"},
        ],
    )
    assert result[0].advertiser_id == "123"


def test_resolve_checks_build_but_viewer_only_gets_blocked(
    session, account_access_case
):
    from app.modules.tenants.models import TenantMembership

    context, grant = account_access_case
    line = InputLine(line_no=42, raw=grant.advertiser_id)
    assert (
        resolve_lines(session, context=context, bc_id=grant.bc_id, lines=[line])[
            0
        ].status
        == "MATCHED"
    )
    session.get(TenantMembership, (context.tenant_id, context.actor_id)).role = "viewer"
    session.flush()
    result = resolve_lines(session, context=context, bc_id=grant.bc_id, lines=[line])[0]
    assert result.status == "BLOCKED" and result.reason == "action_forbidden"
    assert result.line_no == 42 and result.advertiser_id == grant.advertiser_id


def test_other_bc_exact_id_diagnosis_without_cross_tenant_leak(
    session, account_access_case, other_context
):
    from app.modules.accounts.models import TenantBC

    context, grant = account_access_case
    session.add(TenantBC(tenant_id=context.tenant_id, bc_id="other"))
    session.add(TenantBC(tenant_id=other_context.tenant_id, bc_id="other"))
    session.flush()
    line = InputLine(line_no=1, raw=grant.advertiser_id)
    own = resolve_lines(session, context=context, bc_id="other", lines=[line])[0]
    foreign = resolve_lines(
        session, context=other_context, bc_id="other", lines=[line]
    )[0]
    assert own.status == "BLOCKED" and own.reason == "account_not_in_bc"
    assert foreign.status == "NOT_FOUND" and foreign.advertiser_id is None


def test_database_same_name_ambiguity_and_bc_scope(session, account_access_case):
    from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess, TenantBC

    context, grant = account_access_case
    account = session.get(AdvertiserAccount, (context.tenant_id, grant.advertiser_id))
    account.name = "same name"
    session.add(
        AdvertiserAccount(
            tenant_id=context.tenant_id,
            advertiser_id="2",
            name="same name",
            currency="USD",
            timezone="UTC",
            remote_status="STATUS_ENABLE",
        )
    )
    session.add(
        AdvertiserAccount(
            tenant_id=context.tenant_id, advertiser_id="outside", name="same name"
        )
    )
    session.add(TenantBC(tenant_id=context.tenant_id, bc_id="outside"))
    session.flush()
    session.add(BCAccountAccess(**{**grant.model_dump(), "advertiser_id": "2"}))
    session.add(
        BCAccountAccess(
            **{**grant.model_dump(), "advertiser_id": "outside", "bc_id": "outside"}
        )
    )
    session.flush()
    result = resolve_lines(
        session,
        context=context,
        bc_id=grant.bc_id,
        lines=[InputLine(line_no=5, raw="same name")],
    )[0]
    assert result.status == "AMBIGUOUS"
    assert result.candidates == ["2", grant.advertiser_id]
