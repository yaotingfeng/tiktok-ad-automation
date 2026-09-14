"""以真实 PG 锁和服务 SQL 量化发布器并行度及密集/稀疏租户公平性。"""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import perf_counter
from uuid import uuid4

import pytest
from sqlalchemy import event, insert
from sqlmodel import Session, col, select

from app.jobs import outbox
from app.jobs.models import DispatchTenantCursor, PendingDispatch
from tests.modules.strategies.test_concurrency import (
    isolated_strategy_database as isolated_strategy_database,
)


@pytest.fixture
def publisher_case(isolated_strategy_database, monkeypatch):
    db, context, _ = isolated_strategy_database
    monkeypatch.setattr(outbox, "engine", db)
    sent = []
    monkeypatch.setattr(
        outbox.celery_app, "send_task", lambda _name, **kwargs: sent.append(kwargs)
    )
    return db, context, sent


def seed(db, *, tenants=100, messages=5, inactive=0):
    identities = [uuid4() for _ in range(tenants + inactive)]
    with db.begin() as connection:
        connection.execute(
            insert(DispatchTenantCursor), [{"tenant_id": t} for t in identities]
        )
        connection.execute(
            insert(PendingDispatch),
            [
                {
                    "tenant_id": tenant,
                    "actor_id": tenant,
                    "task_name": "jobs.probe",
                    "task_key": f"publisher-load-{i}",
                    "payload": {},
                }
                for tenant in identities[:tenants]
                for i in range(messages)
            ],
        )
        connection.exec_driver_sql("ANALYZE pending_dispatch")
        connection.exec_driver_sql("ANALYZE dispatch_tenant_cursor")


def test_parallel_publisher_can_serve_tenants_not_yet_used_by_first(
    publisher_case, monkeypatch
):
    db, _, sent = publisher_case
    seed(db)
    first_send, release = Event(), Event()

    def publish(_name, **kwargs):
        sent.append(kwargs)
        if not first_send.is_set():
            first_send.set()
            assert release.wait(5)

    monkeypatch.setattr(outbox.celery_app, "send_task", publish)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(outbox.flush_dispatch)
        try:
            assert first_send.wait(5)
            second = pool.submit(outbox.flush_dispatch)
            assert second.result(timeout=4) == 100
        finally:
            release.set()
        assert first.result(timeout=5) == 100
    assert len(sent) == len({message["task_id"] for message in sent}) == 200
    counts = Counter(message["kwargs"]["tenant_id"] for message in sent)
    assert len(counts) == 40 and set(counts.values()) == {5}


def test_sparse_publisher_continues_past_one_fully_locked_candidate_page(
    publisher_case, monkeypatch
):
    db, _, sent = publisher_case
    seed(db, tenants=200, messages=1)
    paused, release = Event(), Event()

    def publish(_name, **kwargs):
        sent.append(kwargs)
        if len(sent) == 100:
            paused.set()
            assert release.wait(5)

    monkeypatch.setattr(outbox.celery_app, "send_task", publish)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(outbox.flush_dispatch)
        try:
            assert paused.wait(5)
            assert pool.submit(outbox.flush_dispatch).result(timeout=4) == 100
        finally:
            release.set()
        assert first.result(timeout=5) == 100
    assert len(sent) == len({message["task_id"] for message in sent}) == 200
    assert len({message["kwargs"]["tenant_id"] for message in sent}) == 200


def test_locked_candidate_scan_stops_after_ten_pages(publisher_case, record_property):
    db, _, sent = publisher_case
    seed(db, tenants=1001, messages=1)
    queries = []

    def count_sql(_conn, _cursor, statement, _parameters, _context, _many):
        queries.append(statement)

    with Session(db) as holder, holder.begin():
        holder.exec(
            select(DispatchTenantCursor)
            .order_by(col(DispatchTenantCursor.tenant_id))
            .limit(1000)
            .with_for_update()
        ).all()
        started = perf_counter()
        event.listen(db, "before_cursor_execute", count_sql)
        try:
            assert outbox.flush_dispatch() == 0
        finally:
            event.remove(db, "before_cursor_execute", count_sql)
        record_property("locked_candidate_seconds", round(perf_counter() - started, 4))
    assert not sent
    assert (
        sum(
            "ORDER BY dispatch_tenant_cursor.last_published_at" in statement
            for statement in queries
        )
        == 10
    )
    assert sum("FOR UPDATE SKIP LOCKED" in statement for statement in queries) == 1000
    # 锁释放后的下一轮正常推进，不能把跳锁误记为已服务或丢弃消息。
    assert outbox.flush_dispatch() == 100
    assert len(sent) == len({message["task_id"] for message in sent}) == 100


@pytest.mark.parametrize("messages,inactive", [(5, 0), (1, 0), (5, 10000), (1, 10000)])
def test_single_publisher_retains_capacity_and_bounded_fairness(
    publisher_case, record_property, messages, inactive
):
    db, _, sent = publisher_case
    seed(db, messages=messages, inactive=inactive)
    queries = []

    def count_sql(_conn, _cursor, statement, _parameters, _context, _many):
        queries.append(statement)

    started = perf_counter()
    event.listen(db, "before_cursor_execute", count_sql)
    try:
        published = outbox.flush_dispatch()
    finally:
        event.remove(db, "before_cursor_execute", count_sql)
    counts = Counter(message["kwargs"]["tenant_id"] for message in sent)
    metrics = {
        "messages_per_tenant": messages,
        "inactive_tenants": inactive,
        "published": published,
        "served_tenants": len(counts),
        "sql_count": len(queries),
        "seconds": round(perf_counter() - started, 4),
    }
    for key, value in metrics.items():
        record_property(key, value)
    assert published == len(sent) == 100, metrics
    assert len(counts) == (20 if messages == 5 else 100), metrics
    assert set(counts.values()) == {messages}
    assert len({message["task_id"] for message in sent}) == 100
    # 候选集合每轮只排序一次，不能为了逐租户锁重复扫描历史游标集合。
    assert (
        sum(
            "ORDER BY dispatch_tenant_cursor.last_published_at" in statement
            for statement in queries
        )
        == 1
    )
