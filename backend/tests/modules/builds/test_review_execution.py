"""Independent execution audit against real PostgreSQL and official SDK wire."""

import json

from sqlalchemy import text
from sqlmodel import Session, select
from urllib3.response import HTTPResponse

from app.modules.builds.execution_models import ExecutionStep, StepEvidence
from tests.modules.builds.test_execution import executable as executable
from tests.modules.builds.test_execution import run


def success_wire(monkeypatch):
    calls = []

    def request(_pool, method, url, **kwargs):
        assert method == "POST"
        body = json.loads(kwargs["body"])
        kind = "cta" if "portfolio" in url else url.split("/")[-3]
        key = {
            "cta": "creative_portfolio_id",
            "campaign": "campaign_id",
            "adgroup": "adgroup_id",
            "ad": "smart_plus_ad_id",
        }[kind]
        remote_id = f"actual-{kind}-{len(calls)}"
        calls.append((kind, body, remote_id))
        return HTTPResponse(
            body=json.dumps(
                {
                    "code": 0,
                    "data": {key: remote_id, "operation_status": "ENABLE"},
                    "request_id": "review-offline",
                }
            ).encode(),
            status=200,
        )

    monkeypatch.setattr("urllib3.PoolManager.request", request)
    return calls


def test_review_ad_binds_actual_cta_in_documented_ad_configuration(
    executable, redis_client, monkeypatch
):
    """Official doc1843317390059522: CTA is nested in ad_configuration."""
    calls = success_wire(monkeypatch)
    for kind in ("MATERIAL", "CTA", "CAMPAIGN", "ADGROUP", "AD"):
        run(executable, redis_client, kind)
    cta_id = next(remote_id for kind, _, remote_id in calls if kind == "cta")
    ads = [body for kind, body, _ in calls if kind == "ad"]
    assert ads
    for body in ads:
        assert body.get("ad_configuration", {}).get("call_to_action_id") == cta_id
        assert "call_to_action_id" not in body


def test_review_database_commit_failure_preserves_received_cta_id(
    executable, redis_client, monkeypatch
):
    """PostgreSQL defers a real rejection until commit, rolling back the receipt."""
    db, _, ids = executable
    with db.begin() as connection:
        connection.execute(
            text("""
            CREATE FUNCTION review_reject_created_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              IF NEW.kind='CTA' AND NEW.status='SUCCEEDED' THEN
                RAISE EXCEPTION 'review deferred receipt rejection' USING ERRCODE='23514';
              END IF;
              RETURN NEW;
            END $$
        """)
        )
        connection.execute(
            text("""
            CREATE CONSTRAINT TRIGGER review_deferred_receipt
            AFTER UPDATE ON execution_step DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION review_reject_created_receipt()
        """)
        )
    calls = success_wire(monkeypatch)
    run(executable, redis_client, "CTA")
    run(executable, redis_client, "CTA")
    assert len(calls) == 1
    with Session(db) as session:
        step = session.get(ExecutionStep, ids["CTA"][0])
        assert step.status == "UNKNOWN" and step.remote_id is None
        evidence = session.exec(
            select(StepEvidence).where(StepEvidence.step_id == step.id)
        ).all()
        # A known CTA ID must survive in append-only evidence because there is no
        # unknown-ID list endpoint. Recovery may not discard this successful GET key.
        assert any(row.summary.get("remote_id") == calls[0][2] for row in evidence)
