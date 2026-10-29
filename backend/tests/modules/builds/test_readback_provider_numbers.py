from copy import deepcopy

import pytest

from app.integrations.tiktok.contracts.builds import BuildReadQuery
from app.integrations.tiktok.contracts.common import CallEvidence, McpBusinessResponse
from app.modules.builds.readback_compare import compare_record, parse_page
from app.modules.builds.request_compiler import decode_intent
from tests.contracts.test_tiktok_build_contract import build_bodies
from tests.modules.builds.test_reconciliation import (  # noqa: F401
    arm,
    run,
    wire,
)
from tests.modules.builds.test_reconciliation import (
    recon_env as recon_env,
)


def read(kind, changes):
    body = deepcopy(build_bodies()[kind])
    if kind == "ADGROUP":
        body["roas_bid"] = "1.06"
    intent = decode_intent(kind, body)
    key = {"ADGROUP": "adgroup_id", "CTA": "creative_portfolio_id"}[kind]
    row = {**body, key: "remote-id", **changes}
    if kind == "CTA":
        row.pop("advertiser_id")
        data = row
    else:
        data = {
            "list": [row],
            "page_info": {
                "page": 1,
                "page_size": 100,
                "total_page": 1,
                "total_number": 1,
            },
        }
    query = BuildReadQuery(intent=intent, remote_id="remote-id", page=1)
    page = parse_page(
        query=query,
        response=McpBusinessResponse(
            data=data, evidence=CallEvidence(request_id="read-1")
        ),
    )
    return query, page.rows[0]


def test_provider_decimal_number_is_compared_without_losing_observed_value(
    recon_env, monkeypatch
):
    identity, body = arm(recon_env, "ADGROUP")
    wire(monkeypatch, [{**body, "adgroup_id": "remote-id", "roas_bid": 1.2}])
    assert run(recon_env, identity).state == "SUCCEEDED"


@pytest.mark.parametrize("value", [True, float("nan"), float("inf")])
def test_invalid_provider_number_stays_unverified(value):
    query, record = read("ADGROUP", {"roas_bid": value})
    assert compare_record(query=query, record=record) == "INCOMPLETE"


def test_cta_get_uses_account_request_scope_and_actual_returned_assets():
    query, record = read("CTA", {})
    assert compare_record(query=query, record=record) == "MATCH"
    query, record = read("CTA", {"portfolio_content": []})
    assert compare_record(query=query, record=record) != "MATCH"


@pytest.mark.parametrize("different_assets", [False, True])
def test_unknown_cta_reuses_verified_same_scope_after_readback(
    recon_env, monkeypatch, different_assets
):
    from uuid import uuid4

    from sqlmodel import Session, select

    from app.modules.builds.execution_models import ExecutionStep, StepEvidence
    from app.modules.builds.execution_state import evidence

    env = recon_env
    identity, body = arm(env, "CTA")
    with Session(env.engine) as db, db.begin():
        source = db.get(ExecutionStep, identity)
        values = source.model_dump(exclude={"id", "attempt_id"})
        values.update(
            step_key="verified-cta-reuse",
            status="SUCCEEDED",
            remote_id="existing-cta",
            attempt=0,
        )
        candidate = ExecutionStep(id=uuid4(), **values)
        db.add(candidate)
        db.flush()
        evidence(
            db,
            step=candidate,
            claim=None,
            conclusion="CREATED",
            summary={"remote_id": "existing-cta"},
        )
        candidate_id = candidate.id
    actual = {**body, "creative_portfolio_id": "existing-cta"}
    actual.pop("advertiser_id")
    if different_assets:
        actual["portfolio_content"] = [
            {"asset_ids": ["another"], "asset_content": "Different"}
        ]
    calls = wire(monkeypatch, actual)
    if different_assets:
        assert run(env, identity).state == "UNKNOWN"
        with Session(env.engine) as db:
            assert db.get(ExecutionStep, identity).remote_id is None
        return
    assert run(env, identity).state == "SUCCEEDED"
    assert len(calls) == 1
    assert calls[0][1]["creative_portfolio_id"] == "existing-cta"
    with Session(env.engine) as db:
        row = db.exec(
            select(StepEvidence).where(
                StepEvidence.step_id == identity,
                StepEvidence.conclusion == "RECONCILED",
            )
        ).one()
        assert row.summary["reused_cta_step_id"] == str(candidate_id)
        assert db.get(ExecutionStep, identity).request_body == body
