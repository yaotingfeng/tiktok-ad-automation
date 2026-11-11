from datetime import UTC, datetime
from uuid import uuid4

from app.modules.ai_workbench.ai_tasks import _check_fence
from app.modules.ai_workbench.knowledge_refs import (
    sanitize_public,
    snapshot_knowledge_refs,
)
from app.modules.ai_workbench.reviews import DSHReviewTask


def test_frozen_refs_remove_credentials_and_model_trace():
    value = sanitize_public({"token": "secret", "nested": {"reasoning": "hidden", "value": 3}})
    assert value == {"nested": {"value": 3}}
    refs = snapshot_knowledge_refs([
        {"key": "rules", "version": 4, "source": "library", "metadata": {"api_key": "x", "label": "public"}},
        {"key": "missing-version"},
    ])
    assert refs == [{"key": "rules", "version": "4", "source": "library", "metadata": {"label": "public"}}]


def test_review_fence_rejects_stale_worker():
    task = DSHReviewTask(
        tenant_id=uuid4(), optimizer_id=uuid4(), request_id=uuid4(), dsh_task_id="dsh:test",
        authorization_expires_at=datetime.now(UTC), fence="current", optimizer_version=1,
        created_by=uuid4(),
    )
    try:
        _check_fence(task, fence="old")
    except Exception as error:
        assert getattr(error, "code", None) == "ai_task_fence_conflict"
    else:
        raise AssertionError("stale worker fence must be rejected")


def test_sanitizer_removes_provider_credentials_and_reasoning_trace():
    assert sanitize_public({"provider_password": "x", "provider_credential": "x", "reasoning_trace": "x", "ok": 1}) == {"ok": 1}


def test_nested_rule_scope_is_checked():
    from decimal import Decimal

    from app.modules.ai_workbench.models import AIOptimizerVersion
    from app.modules.ai_workbench.proposals import _within_authorization
    version = AIOptimizerVersion(tenant_id=uuid4(), optimizer_id=uuid4(), number=1, daily_spend=Decimal("1"), priority="BALANCED", bc_id="bc-a", advertiser_ids=["acc-1"], optimization_mode="AUTO", authorization_limits={"rule": True}, strategy_binding_id=uuid4(), request_id=uuid4(), request_digest="x"*64, created_by=uuid4())
    assert _within_authorization(version=version, kind="rule", before={}, after={"definition": {"accounts": {"advertiser_ids": ["acc-2"]}}})[0] is False
