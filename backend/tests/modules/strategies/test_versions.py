from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlmodel import select

from app.core.errors import DomainError
from app.modules.strategies.copy_pool import POOL_VERSION
from app.modules.strategies.models import CopyEntry, Strategy, StrategyVersion
from app.modules.strategies.schemas import StrategyConfig
from app.modules.strategies.service import (
    append_version,
    create_strategy,
    get_version,
    set_active,
)
from app.modules.tenants.models import TenantMembership


def config(**changes):
    return StrategyConfig.model_validate(
        {
            "budget": "100.25",
            "currency": "USD",
            "target_roas": "1.08",
            "group_size": 10,
            "creative_count": 2,
            "copy_pool_version": POOL_VERSION,
        }
        | changes
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"budget": 100.25},
        {"budget": "NaN"},
        {"budget": "Infinity"},
        {"budget": "-1"},
        {"target_roas": True},
        {"group_size": True},
        {"creative_count": 1.5},
        {"currency": "usd"},
        {"account_pool": ["a"]},
        {"rotation": 10},
        {"budget": "1.0000000000001"},
    ],
)
def test_config_rejects_lossy_or_obsolete_values(changes):
    with pytest.raises(ValidationError):
        config(**changes)


def test_versions_preserve_decimal_budget_and_isolation(
    session, context, other_context
):
    identity = create_strategy(
        session, context=context, name=" 普通短剧 ", config=config()
    )
    original = session.exec(
        select(StrategyVersion).where(StrategyVersion.strategy_id == identity)
    ).one()
    new_id = append_version(
        session, context=context, strategy_id=identity, config=config(budget="200.00")
    )
    assert get_version(session, context=context, version_id=new_id).budget == Decimal(
        "200.00"
    )
    assert get_version(
        session, context=context, version_id=original.id
    ).budget == Decimal("100.25")
    assert original.budget == Decimal("100.25")
    assert session.get(Strategy, identity).name == "普通短剧"
    with pytest.raises(DomainError, match="strategy_not_found"):
        get_version(session, context=other_context, version_id=new_id)
    session.flush()


def test_mutations_are_idempotent_and_detect_changed_intent(session, context):
    request = uuid4()
    identity = create_strategy(
        session, context=context, name="S", config=config(), request_id=request
    )
    assert (
        create_strategy(
            session, context=context, name="S", config=config(), request_id=request
        )
        == identity
    )
    with pytest.raises(DomainError, match="idempotency_conflict"):
        create_strategy(
            session, context=context, name="S2", config=config(), request_id=request
        )
    append_request = uuid4()
    version = append_version(
        session,
        context=context,
        strategy_id=identity,
        config=config(budget="200"),
        expected_version=1,
        request_id=append_request,
    )
    assert (
        append_version(
            session,
            context=context,
            strategy_id=identity,
            config=config(budget="200"),
            expected_version=1,
            request_id=append_request,
        )
        == version
    )
    with pytest.raises(DomainError, match="version_conflict"):
        append_version(
            session,
            context=context,
            strategy_id=identity,
            config=config(budget="300"),
            expected_version=1,
        )
    assert session.get(Strategy, identity).latest_version == 2


def test_disabled_strategy_keeps_old_version_and_requires_current_permission(
    session, context
):
    identity = create_strategy(session, context=context, name="S", config=config())
    original = session.exec(
        select(StrategyVersion).where(StrategyVersion.strategy_id == identity)
    ).one()
    set_active(session, context=context, strategy_id=identity, active=False)
    assert get_version(
        session, context=context, version_id=original.id
    ).budget == Decimal("100.25")
    with pytest.raises(DomainError, match="strategy_not_found"):
        append_version(session, context=context, strategy_id=identity, config=config())
    member = session.get(TenantMembership, (context.tenant_id, context.actor_id))
    member.role = "viewer"
    session.add(member)
    session.flush()
    with pytest.raises(DomainError) as denied:
        set_active(session, context=context, strategy_id=identity, active=True)
    assert denied.value.code == "action_forbidden"


def test_immutable_records_reject_sql_update_delete_and_late_seed(session, context):
    identity = create_strategy(session, context=context, name="S", config=config())
    original = session.exec(
        select(StrategyVersion).where(StrategyVersion.strategy_id == identity)
    ).one()
    for statement in (
        update(StrategyVersion)
        .where(StrategyVersion.id == original.id)
        .values(budget=999),
        delete(StrategyVersion).where(StrategyVersion.id == original.id),
        update(CopyEntry)
        .where(CopyEntry.pool_version_id == POOL_VERSION)
        .values(text="Changed"),
    ):
        with pytest.raises(DBAPIError), session.begin_nested():
            session.execute(statement)
    with pytest.raises(DBAPIError), session.begin_nested():
        session.add(
            CopyEntry(
                id=uuid4(),
                pool_version_id=POOL_VERSION,
                text="Late entry",
                position=101,
            )
        )
        session.flush()


def test_versions_cannot_reference_other_tenant_strategy(
    session, context, other_context
):
    identity = create_strategy(session, context=context, name="S", config=config())
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            StrategyVersion(
                tenant_id=other_context.tenant_id,
                strategy_id=identity,
                number=2,
                copy_pool_version_id=POOL_VERSION,
                config=config().model_dump(mode="json"),
                budget=Decimal("100.25"),
                target_roas=Decimal("1.08"),
                created_by=other_context.actor_id,
                request_id=uuid4(),
                request_digest="0" * 64,
                request_kind="append",
            )
        )
        session.flush()


@pytest.mark.parametrize(
    "bad_config,budget", [({}, Decimal("100.25")), ({"budget": "NaN"}, Decimal("NaN"))]
)
def test_database_rejects_incomplete_or_nonfinite_version_facts(
    session, context, bad_config, budget
):
    identity = create_strategy(session, context=context, name="S", config=config())
    payload = {} if not bad_config else config().model_dump(mode="json") | bad_config
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            StrategyVersion(
                tenant_id=context.tenant_id,
                strategy_id=identity,
                number=2,
                copy_pool_version_id=POOL_VERSION,
                config=payload,
                budget=budget,
                target_roas=Decimal("1.08"),
                created_by=context.actor_id,
                request_id=uuid4(),
                request_digest="1" * 64,
                request_kind="append",
            )
        )
        session.flush()
