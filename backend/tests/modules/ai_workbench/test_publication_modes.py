from decimal import Decimal
from uuid import uuid4

from app.modules.ai_workbench.models import AIOptimizerVersion
from app.modules.ai_workbench.proposals import _within_authorization


def optimizer_version(*, mode: str, limits: dict) -> AIOptimizerVersion:
    return AIOptimizerVersion(
        tenant_id=uuid4(),
        optimizer_id=uuid4(),
        number=1,
        daily_spend=Decimal("10"),
        target_roas=None,
        priority="BALANCED",
        bc_id="bc-a",
        advertiser_ids=["acc-1"],
        optimization_mode=mode,
        authorization_limits=limits,
        strategy_binding_id=uuid4(),
        request_id=uuid4(),
        request_digest="a" * 64,
        created_by=uuid4(),
    )


def test_auto_publication_is_in_scope_only_when_explicitly_authorized():
    version = optimizer_version(
        mode="AUTO", limits={"strategy": True, "max_budget_delta": 5}
    )
    assert _within_authorization(
        version=version, kind="strategy", before={"budget": 10}, after={"budget": 14}
    ) == (True, None)
    allowed, reason = _within_authorization(
        version=version, kind="strategy", before={"budget": 10}, after={"budget": 20}
    )
    assert allowed is False
    assert reason == "budget_delta_out_of_scope"


def test_manual_mode_does_not_change_authorization_scope():
    version = optimizer_version(mode="MANUAL", limits={"strategy": True})
    assert _within_authorization(
        version=version, kind="strategy", before={}, after={"config": {}}
    ) == (True, None)


def test_rule_scope_rejects_another_bc_or_account():
    version = optimizer_version(mode="AUTO", limits={"rule": True})
    allowed, reason = _within_authorization(
        version=version,
        kind="rule",
        before={},
        after={"bc_id": "bc-other", "advertiser_ids": ["acc-1"]},
    )
    assert allowed is False
    assert reason == "bc_scope_out_of_scope"


def test_nested_strategy_budget_delta_is_checked():
    version = optimizer_version(
        mode="AUTO", limits={"strategy": {"allowed": True, "max_budget_delta": 2}}
    )
    allowed, reason = _within_authorization(
        version=version,
        kind="strategy",
        before={"config": {"budget": 10}},
        after={"config": {"budget": 20}},
    )
    assert allowed is False
    assert reason == "budget_delta_out_of_scope"


def test_auto_unlisted_strategy_fields_and_unbounded_roas_stay_pending():
    version = optimizer_version(
        mode="AUTO", limits={"strategy": True, "max_budget_delta": 5}
    )
    allowed, _ = _within_authorization(
        version=version,
        kind="strategy",
        before={"config": {"budget": 10, "target_roas": 1}},
        after={"config": {"budget": 10, "target_roas": 2}},
    )
    assert not allowed
    version.authorization_limits = {
        "strategy": {
            "allowed": True,
            "allowed_fields": ["target_roas"],
            "max_target_roas_delta": 0.2,
        }
    }
    assert _within_authorization(
        version=version,
        kind="strategy",
        before={"config": {"target_roas": 1}},
        after={"config": {"target_roas": 1.1}},
    ) == (True, None)
    assert not _within_authorization(
        version=version,
        kind="strategy",
        before={"config": {"target_roas": 1}},
        after={"config": {"target_roas": 2}},
    )[0]
