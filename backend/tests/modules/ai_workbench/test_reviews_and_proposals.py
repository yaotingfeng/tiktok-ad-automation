import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from app.modules.ai_workbench.ai_tasks import _check_fence
from app.modules.ai_workbench.knowledge_refs import (
    sanitize_public,
    snapshot_knowledge_refs,
)
from app.modules.ai_workbench.reviews import DSHReviewTask, frozen_evidence_ids


def test_frozen_refs_remove_credentials_and_model_trace():
    value = sanitize_public(
        {"token": "secret", "nested": {"reasoning": "hidden", "value": 3}}
    )
    assert value == {"nested": {"value": 3}}
    refs = snapshot_knowledge_refs(
        [
            {
                "key": "rules",
                "version": 4,
                "source": "library",
                "metadata": {"api_key": "x", "label": "public"},
            },
            {"key": "missing-version"},
        ]
    )
    assert refs == [
        {
            "key": "rules",
            "version": "4",
            "source": "library",
            "metadata": {"label": "public"},
        }
    ]


def test_review_fence_rejects_stale_worker():
    task = DSHReviewTask(
        tenant_id=uuid4(),
        optimizer_id=uuid4(),
        request_id=uuid4(),
        dsh_task_id="dsh:test",
        authorization_expires_at=datetime.now(UTC),
        fence="current",
        optimizer_version=1,
        created_by=uuid4(),
    )
    try:
        _check_fence(task, fence="old")
    except Exception as error:
        assert getattr(error, "code", None) == "ai_task_fence_conflict"
    else:
        raise AssertionError("stale worker fence must be rejected")


def test_terminal_dsh_tasks_are_not_rewritten(monkeypatch):
    from types import SimpleNamespace

    import app.modules.ai_workbench.ai_tasks as ai_tasks

    context = SimpleNamespace(tenant_id=uuid4(), actor_id=uuid4())
    for status in ("COMPLETE", "CANCELLED"):
        task = DSHReviewTask(
            tenant_id=context.tenant_id,
            optimizer_id=uuid4(),
            request_id=uuid4(),
            dsh_task_id="dsh:test",
            authorization_expires_at=datetime.now(UTC),
            fence="current",
            optimizer_version=1,
            status=status,
            cancellation_requested=status == "CANCELLED",
            created_by=context.actor_id,
        )
        monkeypatch.setattr(ai_tasks, "authorize", lambda *args, **kwargs: None)
        monkeypatch.setattr(
            ai_tasks, "_task", lambda *args, _task=task, **kwargs: _task
        )
        session = SimpleNamespace(flush=lambda: None)
        assert (
            ai_tasks.cancel_task(
                session,
                context=context,
                optimizer_id=task.optimizer_id,
                task_id=task.id,
                fence="current",
            ).status
            == status
        )
        if status == "CANCELLED":
            try:
                ai_tasks.cancel_task(
                    session,
                    context=context,
                    optimizer_id=task.optimizer_id,
                    task_id=task.id,
                    fence="stale",
                )
            except Exception as error:
                assert getattr(error, "code", None) == "ai_task_fence_conflict"
            else:
                raise AssertionError("stale cancellation fence must be rejected")
        assert (
            ai_tasks.restart_task(
                session,
                context=context,
                optimizer_id=task.optimizer_id,
                task_id=task.id,
            ).status
            == status
        )


def test_sanitizer_removes_provider_credentials_and_reasoning_trace():
    assert sanitize_public(
        {
            "provider_password": "x",
            "provider_credential": "x",
            "reasoning_trace": "x",
            "ok": 1,
        }
    ) == {"ok": 1}


def test_sanitizer_handles_secret_key_variants_and_json_scalars():
    value = sanitize_public(
        {
            "apiKey": "hidden",
            "refreshToken": "hidden",
            "modelTrace": "hidden",
            "privateKey": "hidden",
            "nested-passphrase": "hidden",
            "chainOfThought": "hidden",
            "id": uuid4(),
            "amount": Decimal("1.25"),
            "payload": b"hello",
        }
    )
    assert set(value) == {"id", "amount", "payload"}
    json.dumps(value)
    assert value["amount"] == "1.25"
    assert value["payload"] == "aGVsbG8="


def test_sanitizer_rejects_non_finite_floats():
    for value in (float("nan"), float("inf"), float("-inf")):
        try:
            sanitize_public(value)
        except ValueError as error:
            assert "non-finite" in str(error)
        else:
            raise AssertionError("non-finite evidence values must be rejected")


def test_nested_rule_scope_is_checked():
    from decimal import Decimal

    from app.modules.ai_workbench.models import AIOptimizerVersion
    from app.modules.ai_workbench.proposals import _within_authorization

    version = AIOptimizerVersion(
        tenant_id=uuid4(),
        optimizer_id=uuid4(),
        number=1,
        daily_spend=Decimal("1"),
        priority="BALANCED",
        bc_id="bc-a",
        advertiser_ids=["acc-1"],
        optimization_mode="AUTO",
        authorization_limits={"rule": True},
        strategy_binding_id=uuid4(),
        request_id=uuid4(),
        request_digest="x" * 64,
        created_by=uuid4(),
    )
    assert (
        _within_authorization(
            version=version,
            kind="rule",
            before={},
            after={
                "definition": {
                    "accounts": {"mode": "ACCOUNTS", "advertiser_ids": ["acc-2"]}
                }
            },
        )[0]
        is False
    )


def test_rule_auto_authorization_fails_closed_for_broad_or_malformed_scopes():
    from app.core.errors import DomainError
    from app.modules.ai_workbench.models import AIOptimizerVersion
    from app.modules.ai_workbench.proposals import _within_authorization

    version = AIOptimizerVersion(
        tenant_id=uuid4(),
        optimizer_id=uuid4(),
        number=1,
        daily_spend=Decimal("1"),
        priority="BALANCED",
        bc_id="bc-a",
        advertiser_ids=["acc-1"],
        optimization_mode="AUTO",
        authorization_limits={"rule": True},
        strategy_binding_id=uuid4(),
        request_id=uuid4(),
        request_digest="x" * 64,
        created_by=uuid4(),
    )
    assert _within_authorization(
        version=version,
        kind="rule",
        before={},
        after={"definition": {"accounts": {"mode": "ALL"}}},
    ) == (False, "account_scope_not_authorized")
    for accounts in (
        {"mode": "BC"},
        {"mode": "ACCOUNTS"},
        {"mode": "ACCOUNTS", "advertiser_ids": ["acc-1"], "bc_ids": ["bc-a"]},
    ):
        try:
            _within_authorization(
                version=version,
                kind="rule",
                before={},
                after={"definition": {"accounts": accounts}},
            )
        except DomainError as error:
            assert error.code == "ai_invalid_rule_scope"
        else:
            raise AssertionError("malformed rule account scope must fail closed")
    assert _within_authorization(
        version=version,
        kind="rule",
        before={},
        after={"definition": {"accounts": {"mode": "BC", "bc_ids": ["bc-a"]}}},
    ) == (True, None)


def test_malformed_nested_rule_scope_raises_domain_error():
    from decimal import Decimal

    from app.core.errors import DomainError
    from app.modules.ai_workbench.models import AIOptimizerVersion
    from app.modules.ai_workbench.proposals import _within_authorization

    version = AIOptimizerVersion(
        tenant_id=uuid4(),
        optimizer_id=uuid4(),
        number=1,
        daily_spend=Decimal("1"),
        priority="BALANCED",
        bc_id="bc-a",
        advertiser_ids=["acc-1"],
        optimization_mode="AUTO",
        authorization_limits={"rule": True},
        strategy_binding_id=uuid4(),
        request_id=uuid4(),
        request_digest="x" * 64,
        created_by=uuid4(),
    )
    for after in (
        {"definition": {"accounts": []}},
        {"definition": {"accounts": {"advertiser_ids": "acc-1"}}},
        {"definition": {"bc_id": []}},
    ):
        try:
            _within_authorization(version=version, kind="rule", before={}, after=after)
        except DomainError as error:
            assert error.code == "ai_invalid_rule_scope"
        else:
            raise AssertionError("malformed nested rule scope must be a DomainError")


def test_frozen_evidence_ids_cover_all_categories():
    ids = {uuid4() for _ in range(8)}
    a, b, c, d, e, f, g, h = (str(item) for item in ids)
    evidence = {
        "plans": [{"id": a}],
        "build_occurrences": [{"id": b, "strategy_version_id": c}],
        "rule_evidence": [{"id": d}],
        "reporting": {"snapshots": [{"id": e, "evidence": [{"version_id": f}]}]},
        "business": {"runs": [{"id": g}], "publications": [{"run_id": h}]},
        "operation_logs": {"coverage": [{"export_id": a}], "exports": [{"id": b}]},
    }
    assert frozen_evidence_ids(evidence) == {a, b, c, d, e, f, g, h}


def test_optimizer_fk_and_proposal_constraints_match_migration():
    from app.modules.ai_workbench.proposals import AIProposal

    for table, name in (
        (DSHReviewTask.__table__, "fk_ai_dsh_review_optimizer"),
        (AIProposal.__table__, "fk_ai_proposal_optimizer"),
    ):
        constraint = next(fk for fk in table.foreign_key_constraints if fk.name == name)
        assert [column.name for column in constraint.columns] == [
            "tenant_id",
            "optimizer_id",
        ]
    assert AIProposal.__table__.c.decision_reason.type.length == 1000
    checks = {
        constraint.name
        for constraint in AIProposal.__table__.constraints
        if constraint.name
    }
    assert {"ck_ai_proposal_kind", "ck_ai_proposal_status"} <= checks
