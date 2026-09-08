from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.errors import DomainError
from app.modules.providers.models import (
    LinkPreparation,
    LinkPreparationItem,
    PromotionLink,
    ProviderApplication,
    ProviderConnection,
    ProviderDrama,
    ProviderEffect,
    ProviderRemoteScope,
)
from app.modules.providers.repository import (
    find_ready_link,
    get_application,
    get_connection,
    list_dramas,
)
from app.modules.providers.schemas import link_reuse_key
from app.modules.tenants.models import TenantMembership


def seed_provider(session, context, *, external_app="same-app", title="Moon"):
    connection = ProviderConnection(
        tenant_id=context.tenant_id,
        kind="wangyan",
        display_name="Local fixture",
        encrypted_credentials="fake-ciphertext",
    )
    session.add(connection)
    session.flush()
    application = ProviderApplication(
        tenant_id=context.tenant_id,
        connection_id=connection.id,
        external_id=external_app,
        name="Application",
    )
    session.add(application)
    session.flush()
    drama = ProviderDrama(
        tenant_id=context.tenant_id,
        connection_id=connection.id,
        application_id=external_app,
        external_drama_id="same-drama",
        title=title,
        language="en",
    )
    session.add(drama)
    session.flush()
    return connection, application, drama


def promotion(context, connection, drama, *, version=1, status="ready", config=None):
    config = {"episode": 1} if config is None else config
    return PromotionLink(
        tenant_id=context.tenant_id,
        connection_id=connection.id,
        application_id=drama.application_id,
        drama_id=drama.id,
        reuse_key=link_reuse_key(
            context.tenant_id,
            connection.id,
            drama.application_id,
            drama.external_drama_id,
            config,
        ),
        config=config,
        version=version,
        remote_id="remote-fixture",
        url="https://example.test/link",
        protected_base="original_base",
        attribution={"source": "remote"},
        verified_at=datetime.now(UTC),
        status=status,
    )


def preparation(session, context, connection, application):
    result = LinkPreparation(
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        connection_id=connection.id,
        application_id=application.external_id,
        request_id=uuid4(),
        request_digest="a" * 64,
        config={},
    )
    session.add(result)
    session.flush()
    item = LinkPreparationItem(
        tenant_id=context.tenant_id,
        preparation_id=result.id,
        line_no=1,
        raw_input=" Moon ",
    )
    session.add(item)
    session.flush()
    return result, item


def test_same_external_identity_and_title_do_not_cross_tenants(
    session, context, other_context
):
    own, _, drama = seed_provider(session, context)
    foreign, _, _ = seed_provider(session, other_context)
    page = list_dramas(
        session,
        context=context,
        connection_id=own.id,
        application_id="same-app",
        title="Moon",
    )
    assert [row.id for row in page.items] == [drama.id]
    with pytest.raises(DomainError) as error:
        get_connection(session, context=context, connection_id=foreign.id)
    assert error.value.code == "resource_not_found"
    with pytest.raises(DomainError):
        list_dramas(
            session,
            context=context,
            connection_id=foreign.id,
            application_id="same-app",
            title="Moon",
        )


def test_read_reloads_membership_and_rejects_revocation(session, context):
    connection, _, _ = seed_provider(session, context)
    assert (
        get_connection(session, context=context, connection_id=connection.id).id
        == connection.id
    )
    membership = session.get(TenantMembership, (context.tenant_id, context.actor_id))
    membership.active = False
    session.flush()
    with pytest.raises(DomainError) as error:
        get_connection(session, context=context, connection_id=connection.id)
    assert error.value.code == "tenant_forbidden"


def test_application_ids_are_external_strings_and_connection_scoped(session, context):
    first, _, _ = seed_provider(session, context, external_app="90071992547409931")
    second, _, _ = seed_provider(session, context, external_app="other-app")
    app = get_application(
        session,
        context=context,
        connection_id=first.id,
        application_id="90071992547409931",
    )
    assert app.external_id == "90071992547409931"
    with pytest.raises(DomainError):
        get_application(
            session,
            context=context,
            connection_id=second.id,
            application_id=app.external_id,
        )


@pytest.mark.parametrize(
    "target", ["application", "drama", "link", "preparation", "item", "scope", "effect"]
)
def test_database_rejects_cross_tenant_relationships(
    session, context, other_context, target
):
    own, own_app, own_drama = seed_provider(session, context)
    foreign, foreign_app, foreign_drama = seed_provider(session, other_context)
    foreign_prep, foreign_item = preparation(
        session, other_context, foreign, foreign_app
    )
    foreign_scope = ProviderRemoteScope(
        tenant_id=other_context.tenant_id, scope_key="foreign-scope"
    )
    session.add(foreign_scope)
    session.flush()
    if target == "application":
        row = ProviderApplication(
            tenant_id=context.tenant_id,
            connection_id=foreign.id,
            external_id="x",
            name="x",
        )
    elif target == "drama":
        row = ProviderDrama(
            tenant_id=context.tenant_id,
            connection_id=foreign.id,
            application_id=foreign_app.external_id,
            external_drama_id="x",
            title="x",
        )
    elif target == "link":
        row = promotion(context, own, foreign_drama)
    elif target == "preparation":
        row = LinkPreparation(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            request_id=uuid4(),
            request_digest="a" * 64,
            connection_id=foreign.id,
            application_id=foreign_app.external_id,
        )
    elif target == "item":
        row = LinkPreparationItem(
            tenant_id=context.tenant_id,
            preparation_id=foreign_prep.id,
            line_no=2,
            raw_input="x",
        )
    elif target == "scope":
        row = ProviderRemoteScope(
            tenant_id=context.tenant_id,
            scope_key="x",
            active_item_id=foreign_item.id,
            status="held",
        )
    else:
        row = ProviderEffect(
            tenant_id=context.tenant_id,
            remote_scope_key=foreign_scope.scope_key,
            step="create",
            request_digest="b" * 64,
        )
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(row)
        session.flush()


def test_database_rejects_same_tenant_wrong_connection_drama(session, context):
    first, _, _ = seed_provider(session, context)
    _, _, other_drama = seed_provider(session, context)
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(promotion(context, first, other_drama))
        session.flush()


def test_ready_partial_unique_keeps_history_and_only_one_current_result(
    session, context
):
    connection, _, drama = seed_provider(session, context)
    first = promotion(context, connection, drama)
    session.add(first)
    session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(promotion(context, connection, drama, version=2))
        session.flush()
    first.status = "superseded"
    session.flush()
    second = promotion(context, connection, drama, version=2)
    session.add(second)
    session.flush()
    current = find_ready_link(
        session,
        context=context,
        connection_id=connection.id,
        application_id=drama.application_id,
        external_drama_id=drama.external_drama_id,
        config={"episode": 1},
    )
    assert current.id == second.id
    assert (
        find_ready_link(
            session,
            context=context,
            connection_id=connection.id,
            application_id=drama.application_id,
            external_drama_id=drama.external_drama_id,
            config={"episode": 2},
        )
        is None
    )
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            promotion(context, connection, drama, version=1, status="superseded")
        )
        session.flush()


def test_preparation_request_and_line_are_unique_in_scope(
    session, context, other_context
):
    connection, application, _ = seed_provider(session, context)
    task, item = preparation(session, context, connection, application)
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            LinkPreparation(
                tenant_id=context.tenant_id,
                actor_id=context.actor_id,
                request_id=task.request_id,
                request_digest="different",
                connection_id=connection.id,
                application_id=application.external_id,
            )
        )
        session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            LinkPreparationItem(
                tenant_id=context.tenant_id,
                preparation_id=task.id,
                line_no=item.line_no,
                raw_input="duplicate",
            )
        )
        session.flush()
    other_connection, other_application, _ = seed_provider(session, other_context)
    session.add(
        LinkPreparation(
            tenant_id=other_context.tenant_id,
            actor_id=other_context.actor_id,
            request_id=task.request_id,
            request_digest="a" * 64,
            connection_id=other_connection.id,
            application_id=other_application.external_id,
        )
    )
    session.flush()


def test_effect_identity_and_remote_scope_are_durable(session, context):
    connection, app, _ = seed_provider(session, context)
    _, item = preparation(session, context, connection, app)
    scope = ProviderRemoteScope(
        tenant_id=context.tenant_id,
        scope_key="channel-key",
        active_item_id=item.id,
        status="held",
    )
    session.add(scope)
    session.flush()
    effect = ProviderEffect(
        tenant_id=context.tenant_id,
        remote_scope_key=scope.scope_key,
        step="create",
        request_digest="a" * 64,
        status="sending",
        attempt_token=uuid4(),
        remote_id="known-id",
        result={"verified": False},
    )
    session.add(effect)
    session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            ProviderEffect(
                tenant_id=context.tenant_id,
                remote_scope_key=scope.scope_key,
                step="create",
                request_digest="a" * 64,
            )
        )
        session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            ProviderRemoteScope(tenant_id=context.tenant_id, scope_key=scope.scope_key)
        )
        session.flush()
    assert effect.remote_id == "known-id" and scope.active_item_id == item.id


def test_dramas_paginate_once_and_cursors_cannot_change_scope(session, context):
    connection, application, first = seed_provider(session, context)
    ids = {first.id}
    for index in range(10):
        row = ProviderDrama(
            tenant_id=context.tenant_id,
            connection_id=connection.id,
            application_id=application.external_id,
            external_drama_id=f"drama-{index}",
            title="Moon",
        )
        ids.add(row.id)
        session.add(row)
    session.flush()
    first_page = list_dramas(
        session,
        context=context,
        connection_id=connection.id,
        application_id=application.external_id,
        title="Moon",
        limit=3,
    )
    cursor = first_page.next_cursor
    assert cursor is not None
    seen = [row.id for row in first_page.items]
    while cursor:
        page = list_dramas(
            session,
            context=context,
            connection_id=connection.id,
            application_id=application.external_id,
            title="Moon",
            limit=3,
            cursor=cursor,
        )
        seen.extend(row.id for row in page.items)
        cursor = page.next_cursor
    assert len(seen) == len(ids) and set(seen) == ids and seen == sorted(seen)
    for invalid in ("!not-base64", first_page.next_cursor):
        with pytest.raises(DomainError) as error:
            list_dramas(
                session,
                context=context,
                connection_id=connection.id,
                application_id=application.external_id,
                title="Other",
                cursor=invalid,
            )
        assert error.value.code == "invalid_cursor"
    other_connection, other_app, _ = seed_provider(session, context)
    with pytest.raises(DomainError):
        list_dramas(
            session,
            context=context,
            connection_id=other_connection.id,
            application_id=other_app.external_id,
            title="Moon",
            cursor=first_page.next_cursor,
        )


@pytest.mark.parametrize("limit", [0, 201, True, "50"])
def test_invalid_limits_are_rejected(session, context, limit):
    connection, application, _ = seed_provider(session, context)
    with pytest.raises(DomainError):
        list_dramas(
            session,
            context=context,
            connection_id=connection.id,
            application_id=application.external_id,
            limit=limit,
        )


def test_connection_model_dump_does_not_expose_encrypted_credentials(session, context):
    connection, _, _ = seed_provider(session, context)
    assert "encrypted_credentials" not in connection.model_dump()
    assert "fake-ciphertext" not in repr(connection)


def test_links_cannot_reuse_a_key_stored_for_different_configuration(session, context):
    connection, _, drama = seed_provider(session, context)
    row = promotion(context, connection, drama)
    row.config = {"episode": 2}
    session.add(row)
    session.flush()
    assert (
        find_ready_link(
            session,
            context=context,
            connection_id=connection.id,
            application_id=drama.application_id,
            external_drama_id=drama.external_drama_id,
            config={"episode": 1},
        )
        is None
    )


def test_real_concurrent_transactions_allow_only_one_current_ready_version():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlalchemy import delete
    from sqlmodel import Session

    from app.core.db import engine
    from app.models import User
    from app.modules.tenants.models import Tenant
    from tests.modules.conftest import create_context

    with Session(engine) as setup:
        context = create_context(setup)
        connection, _, drama = seed_provider(setup, context)
        # Freeze scalar copies before closing a committed SQLAlchemy Session.
        first = promotion(context, connection, drama, version=1)
        second = promotion(context, connection, drama, version=2)
        setup.commit()
    barrier = Barrier(2)

    def insert(row):
        with Session(engine) as transaction:
            barrier.wait(timeout=5)
            try:
                transaction.add(row)
                transaction.commit()
                return "ready"
            except IntegrityError:
                transaction.rollback()
                return "conflict"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(insert, (first, second))) == ["conflict", "ready"]
    finally:
        with Session(engine) as cleanup:
            for model in (
                PromotionLink,
                ProviderDrama,
                ProviderApplication,
                ProviderConnection,
                TenantMembership,
            ):
                cleanup.exec(delete(model).where(model.tenant_id == context.tenant_id))
            cleanup.exec(delete(Tenant).where(Tenant.id == context.tenant_id))
            cleanup.exec(delete(User).where(User.id == context.actor_id))
            cleanup.commit()
