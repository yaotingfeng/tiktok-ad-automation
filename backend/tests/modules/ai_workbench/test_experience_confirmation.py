from uuid import uuid4

import pytest

from app.modules.ai_workbench import service
from app.modules.ai_workbench.reviews import create_review_task
from app.modules.ai_workbench.schemas import OptimizerCreate
from app.modules.tenants.models import TenantMembership
from tests.modules.ai_workbench.test_dispatch_concurrency import (
    dispatch_database,  # noqa: F401
)
from tests.modules.ai_workbench.test_optimizer import (
    api_headers,
    create_body,
    seed_optimizer_dependencies,
)


def test_max_length_candidate_and_validation_confirm_without_loss(
    client, session, context
):
    member = session.get(TenantMembership, (context.tenant_id, context.actor_id))
    member.role = "tenant_admin"
    session.flush()
    strategy = seed_optimizer_dependencies(session, context)
    optimizer = service.create_optimizer(
        session,
        context=context,
        body=OptimizerCreate.model_validate(create_body(strategy.id)),
    )
    task = create_review_task(
        session, context=context, optimizer_id=optimizer.id, request_id=uuid4()
    )
    task.status = "COMPLETE"
    task.result = {
        "experience_candidates": [
            {
                "title": "经验",
                "body": "观察结论",
                "applicability": "适" * 2000,
                "validation": "待验证",
                "evidence_ids": [],
            }
        ]
    }
    session.flush()
    url = f"/api/tenants/{context.tenant_id}/ai-optimizers/{optimizer.id}/reviews/{task.id}/experiences/0/confirm"
    first = client.post(
        url, headers=api_headers(context, session=session), json={"validation_result": "验" * 2000}
    )
    assert first.status_code == 201, first.text
    second = client.post(
        url, headers=api_headers(context, session=session), json={"validation_result": "验" * 2000}
    )
    assert second.json()["id"] == first.json()["id"]
    from uuid import UUID

    from app.modules.ai_workbench.knowledge import KnowledgeVersion

    row = session.get(KnowledgeVersion, UUID(first.json()["id"]))
    assert row.applicability == "适" * 2000
    assert "验" * 2000 in row.body


def test_concurrent_confirmation_creates_one_asset(request, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event
    from types import SimpleNamespace

    from sqlmodel import Session, select

    from app.modules.ai_workbench import api
    from app.modules.ai_workbench.knowledge import KnowledgeVersion
    from tests.modules.conftest import create_context

    database, _, _ = request.getfixturevalue("dispatch_database")
    with Session(database) as session:
        context = create_context(session, role="tenant_admin")
        strategy = seed_optimizer_dependencies(session, context)
        optimizer = service.create_optimizer(
            session,
            context=context,
            body=OptimizerCreate.model_validate(create_body(strategy.id)),
        )
        task = create_review_task(
            session, context=context, optimizer_id=optimizer.id, request_id=uuid4()
        )
        task.status = "COMPLETE"
        task.result = {
            "experience_candidates": [
                {
                    "title": "确认",
                    "body": "结论",
                    "applicability": "条件",
                    "evidence_ids": [],
                }
            ]
        }
        session.commit()
        task_id, optimizer_id = task.id, optimizer.id
    held, release = Event(), Event()
    original = api.create_version

    def delayed(*args, **kwargs):
        held.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(api, "create_version", delayed)

    def confirm():
        with Session(database) as session:
            return api.confirm_ai_experience(
                tenant_id=context.tenant_id,
                optimizer_id=optimizer_id,
                task_id=task_id,
                index=0,
                body=api.ConfirmExperience(validation_result="人工确认"),
                session=session,
                user=SimpleNamespace(id=context.actor_id),
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(confirm)
        assert held.wait(10)
        second = pool.submit(confirm)
        try:
            with pytest.raises(TimeoutError):
                second.result(timeout=0.1)
        finally:
            release.set()
        assert first.result(10)["id"] == second.result(10)["id"]
    with Session(database) as session:
        assert (
            len(
                session.exec(
                    select(KnowledgeVersion).where(
                        KnowledgeVersion.tenant_id == context.tenant_id
                    )
                ).all()
            )
            == 1
        )
