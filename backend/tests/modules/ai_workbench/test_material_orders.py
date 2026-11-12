from uuid import UUID

import httpx

from app.modules.ai_workbench import orders

from .test_completion import setup_plan


def test_order_reuses_frozen_body_on_timeout_and_polls_same_job(
    client, session, context
):
    optimizer, plan, _ = setup_plan(client, session, context)
    order = orders.create_order(
        session,
        context=context,
        row_id=plan.rows[0].id,
        config={"strategy_ids": ["strategy-1"], "feishu_token": "sheet-1"},
    )
    sent = []

    def transport(request):
        if request.url.path == "/api/strategy-catalog":
            return httpx.Response(
                200,
                json={
                    "strategies": [{"id": "strategy-1", "enabled": True, "name": "CL1"}]
                },
            )
        if request.url.path.endswith("/capabilities"):
            return httpx.Response(200, json={"executionMode": "full_series_v2"})
        if request.url.path == "/api/schedules":
            sent.append((request.headers["Idempotency-Key"], request.content))
            if len(sent) == 1:
                raise httpx.ReadTimeout("unknown")
            return httpx.Response(
                201, json={"job": {"id": "batch-1", "status": "queued"}}
            )
        assert request.url.path == "/api/jobs/batch-1"
        return httpx.Response(
            200,
            json={
                "job": {
                    "id": "batch-1",
                    "progressItems": [
                        {
                            "title": order.title,
                            "status": "succeeded",
                            "delivery": {"status": "succeeded"},
                        }
                    ],
                }
            },
        )

    remote = orders.ProductionClient(
        "test-company-key", transport=httpx.MockTransport(transport)
    )
    orders.process_order(session, context=context, order_id=order.id, remote=remote)
    assert order.status == "UNKNOWN"
    orders.process_order(session, context=context, order_id=order.id, remote=remote)
    assert order.job_id == "batch-1"
    assert sent[0] == sent[1]
    orders.process_order(session, context=context, order_id=order.id, remote=remote)
    assert order.status == "COMPLETE"
    assert len(sent) == 2
    assert (
        orders.create_order(
            session,
            context=context,
            row_id=plan.rows[0].id,
            config={"strategy_ids": ["strategy-1"], "feishu_token": "sheet-1"},
        ).id
        == order.id
    )


def test_order_intent_is_not_reported_as_submitted(client, session, context):
    from app.modules.ai_workbench.daily_report import build_daily_report

    optimizer, plan, _ = setup_plan(client, session, context)
    report = build_daily_report(
        session,
        context=context,
        optimizer_id=UUID(optimizer["id"]),
        business_date=plan.business_date,
    )
    assert report["material"]["requested"] == 0
    assert report["material"]["intent"] == 1


def test_scanner_creates_and_enqueues_configured_order_in_real_database(
    client, session, context
):
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from sqlmodel import select

    from app.modules.ai_workbench.orders import MaterialOrder, scan_orders
    from app.modules.ai_workbench.plans import AIDailyPlanRow
    from app.modules.ai_workbench.schemas import OptimizerUpdate
    from app.modules.ai_workbench.service import update_optimizer

    optimizer, plan, _ = setup_plan(client, session, context)
    row = session.get(AIDailyPlanRow, plan.rows[0].id)
    row.business_date = datetime.now(UTC).date() + timedelta(days=1)
    session.add(row)
    session.flush()
    update_optimizer(
        session,
        context=context,
        optimizer_id=UUID(optimizer["id"]),
        body=OptimizerUpdate(
            request_id=uuid4(),
            material_config={"strategy_ids": ["strategy-1"], "feishu_token": "sheet-1"},
        ),
    )
    scan_orders(session, now=datetime.now(UTC))
    assert scan_orders(session, now=datetime.now(UTC) + timedelta(seconds=1)) == 1
    order = session.exec(
        select(MaterialOrder).where(MaterialOrder.plan_row_id == row.id)
    ).one()
    assert order.status == "PENDING"
    assert order.dispatch_generation == 1


def test_paused_order_is_excluded_before_bounded_scan(client, session, context):
    from datetime import UTC, datetime, timedelta

    from app.modules.ai_workbench.models import AIOptimizer

    optimizer, plan, _ = setup_plan(client, session, context)
    order = orders.create_order(
        session,
        context=context,
        row_id=plan.rows[0].id,
        config={"strategy_ids": ["s1"], "feishu_token": "sheet"},
    )
    saved = session.get(AIOptimizer, UUID(optimizer["id"]))
    saved.active = False
    session.add(saved)
    session.flush()
    assert (
        orders.scan_orders(
            session, now=datetime.now(UTC) + timedelta(seconds=1), limit=1
        )
        == 0
    )
    assert order.dispatch_generation == 0
