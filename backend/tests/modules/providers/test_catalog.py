from datetime import timedelta

import pytest

from app.core.errors import DomainError
from app.core.security import create_access_token
from app.modules.providers.catalog import list_links, preparation_summary
from app.modules.providers.models import LinkPreparationItem
from app.modules.providers.service import get_link_results
from tests.modules.providers.test_tenant_repository import (
    preparation,
    promotion,
    seed_provider,
)


def test_link_history_is_scoped_filtered_and_paged(session, context, other_context):
    connection, app, drama = seed_provider(session, context, title="Moon%_")
    expected = []
    for i in range(205):
        link = promotion(context, connection, drama, config={"episode": i + 1})
        session.add(link)
        expected.append(link.id)
    other_connection, _, other_drama = seed_provider(
        session, other_context, title="Moon%_"
    )
    session.add(promotion(other_context, other_connection, other_drama))
    session.flush()
    cursor, found, sizes = None, [], []
    while True:
        page = list_links(
            session,
            context=context,
            connection_id=connection.id,
            application_id=app.external_id,
            query="Moon%_",
            cursor=cursor,
            limit=100,
        )
        sizes.append(len(page.items))
        found.extend(row.link_id for row in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert sizes == [100, 100, 5]
    assert found == sorted(expected)
    assert list_links(session, context=context, query="Moonx").items == []
    cursor = list_links(session, context=context, limit=50).next_cursor
    with pytest.raises(DomainError) as mismatch:
        list_links(session, context=other_context, cursor=cursor)
    assert mismatch.value.code == "invalid_cursor"


def test_preparation_summary_counts_all_rows_without_loading_them(session, context):
    connection, app, _ = seed_provider(session, context)
    prep, first = preparation(session, context, connection, app)
    prep.config = {"episode": 2, "password": "never-public"}
    session.add(prep)
    for i in range(1, 206):
        row = (
            first
            if i == 1
            else LinkPreparationItem(
                tenant_id=context.tenant_id,
                preparation_id=prep.id,
                line_no=i,
                raw_input=f"Moon {i}",
            )
        )
        row.status = "config_conflict" if i <= 101 else "resolving"
        row.resolved = {
            "input_id": str(row.id),
            "line_no": i,
            "raw_input": row.raw_input,
            "provider_kind": "wangyan",
            "connection_id": str(connection.id),
            "application_id": app.external_id,
            "status": "config_conflict" if i <= 101 else "pending",
            "_work": {
                "stage": "read_before",
                "existing_config": {"drama_num": 1, "token": "never-public"},
            },
        }
        session.add(row)
    session.flush()
    summary = preparation_summary(session, context=context, task_id=prep.id)
    assert (summary.total_count, summary.pending_count, summary.exception_count) == (
        205,
        104,
        101,
    )
    assert summary.config == {"episode": 2}
    assert summary.config_display_incomplete is True
    first_page = get_link_results(
        session, context=context, task_id=prep.id, exceptions_only=True
    )
    assert len(first_page.items) == 100 and first_page.next_cursor
    assert first_page.items[0].existing_config == {"episode": 1}
    assert first_page.items[0].requested_config == {"episode": 2}
    assert "never-public" not in first_page.model_dump_json()
    assert "_work" not in first_page.model_dump_json()
    second = get_link_results(
        session,
        context=context,
        task_id=prep.id,
        exceptions_only=True,
        cursor=first_page.next_cursor,
    )
    assert len(second.items) == 1 and second.items[0].line_no == 101
    with pytest.raises(DomainError) as mismatch:
        get_link_results(
            session, context=context, task_id=prep.id, cursor=first_page.next_cursor
        )
    assert mismatch.value.code == "invalid_cursor"
    assert (
        len(
            get_link_results(
                session, context=context, task_id=prep.id, status="pending"
            ).items
        )
        == 100
    )


def test_history_summary_and_connection_filters_are_readonly_http(
    client, session, context, other_context
):
    connection, app, drama = seed_provider(session, context, title="Moon")
    link = promotion(context, connection, drama)
    session.add(link)
    prep, _ = preparation(session, context, connection, app)
    session.flush()
    auth = {
        "Authorization": "Bearer "
        + create_access_token(context.actor_id, timedelta(minutes=5))
    }
    base = f"/api/tenants/{context.tenant_id}/providers"
    assert client.get(f"{base}/links", headers=auth).json()["items"][0][
        "link_id"
    ] == str(link.id)
    assert (
        client.get(f"{base}/links/{link.id}", headers=auth).json()["protected_base"]
        == "original_base"
    )
    assert (
        client.get(f"{base}/link-preparations/{prep.id}/summary", headers=auth).json()[
            "total_count"
        ]
        == 1
    )
    assert (
        client.get(f"{base}/connections?kind=jiashu", headers=auth).json()["items"]
        == []
    )
    assert (
        len(
            client.get(
                f"{base}/connections?query=fixture&status=pending", headers=auth
            ).json()["items"]
        )
        == 1
    )
    outsider = {
        "Authorization": "Bearer "
        + create_access_token(other_context.actor_id, timedelta(minutes=5))
    }
    assert (
        client.get(
            f"/api/tenants/{other_context.tenant_id}/providers/links/{link.id}",
            headers=outsider,
        ).status_code
        == 404
    )
