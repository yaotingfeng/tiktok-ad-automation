from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

from sqlalchemy import delete
from sqlmodel import Session

from app.core.db import engine
from app.models import User
from app.modules.providers.link_steps import claim_effect
from app.modules.providers.models import (
    LinkPreparation,
    LinkPreparationItem,
    ProviderApplication,
    ProviderConnection,
    ProviderDrama,
    ProviderEffect,
    ProviderRemoteScope,
)
from app.modules.providers.repository import claim_remote_scope
from app.modules.tenants.models import Tenant, TenantMembership
from tests.modules.conftest import create_context
from tests.modules.providers.test_tenant_repository import preparation, seed_provider


def test_postgres_concurrent_effect_claim_has_exactly_one_sender():
    with Session(engine) as setup:
        context = create_context(setup)
        connection, application, _ = seed_provider(setup, context)
        _, item = preparation(setup, context, connection, application)
        scope = ProviderRemoteScope(
            tenant_id=context.tenant_id,
            scope_key="concurrent-channel",
            status="held",
            active_item_id=item.id,
        )
        setup.add(scope)
        setup.flush()
        effect = ProviderEffect(
            tenant_id=context.tenant_id,
            remote_scope_key=scope.scope_key,
            step="create",
            request_digest="a" * 64,
        )
        setup.add(effect)
        setup.flush()
        effect_id = effect.id
        setup.commit()
    barrier = Barrier(2)

    def claim(_):
        with Session(engine) as transaction:
            barrier.wait(timeout=5)
            granted = claim_effect(
                transaction,
                effect_id=effect_id,
                tenant_id=context.tenant_id,
                attempt_token=uuid4(),
            )
            transaction.commit()
            return granted

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(claim, range(2))) == [False, True]
    finally:
        cleanup_workflow(context.tenant_id, context.actor_id)


def cleanup_workflow(tenant_id, actor_id):
    with Session(engine) as cleanup:
        for model in (
            ProviderEffect,
            ProviderRemoteScope,
            LinkPreparationItem,
            LinkPreparation,
            ProviderDrama,
            ProviderApplication,
            ProviderConnection,
            TenantMembership,
        ):
            cleanup.exec(delete(model).where(model.tenant_id == tenant_id))
        cleanup.exec(delete(Tenant).where(Tenant.id == tenant_id))
        cleanup.exec(delete(User).where(User.id == actor_id))
        cleanup.commit()


def test_remote_scope_serializes_different_config_requests():
    with Session(engine) as setup:
        context = create_context(setup)
        connection, app, _ = seed_provider(setup, context)
        _, first = preparation(setup, context, connection, app)
        _, second = preparation(setup, context, connection, app)
        ids = (first.id, second.id)
        setup.commit()
    barrier = Barrier(2)

    def claim(item_id):
        with Session(engine) as transaction:
            barrier.wait(timeout=5)
            granted = claim_remote_scope(
                transaction,
                tenant_id=context.tenant_id,
                scope_key="same-channel-for-episode-1-and-2",
                item_id=item_id,
            )
            transaction.commit()
            return granted

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(claim, ids)) == [False, True]
    finally:
        cleanup_workflow(context.tenant_id, context.actor_id)
