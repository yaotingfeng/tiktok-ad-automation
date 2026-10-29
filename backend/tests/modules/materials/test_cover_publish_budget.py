"""真实 PG：空图片探测不应持有素材锁或重复执行整批发布检查。"""

import json
import logging

from sqlalchemy import Engine, event

from app.modules.materials import cover_sharing
from tests.modules.materials.test_cover_throughput import drive, image, matrix, page
from tests.modules.materials.test_covers import job_state
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire


def test_empty_probe_does_not_take_publication_locks(
    source_env, redis_client, wire, monkeypatch, caplog
):
    # Alembic测试迁移的fileConfig会禁用已注册logger；恢复本例日志采集。
    monkeypatch.setattr(cover_sharing.logger, "disabled", False)
    caplog.set_level(logging.INFO, logger=cover_sharing.__name__)
    identities = matrix(source_env, 2, 2)
    empty_queries = []
    publish = cover_sharing._publish

    def observed_publish(db, context, batch, claims, found):
        queries = []

        def observed(_conn, _cursor, statement, *_args):
            queries.append(statement)

        event.listen(Engine, "before_cursor_execute", observed)
        try:
            return publish(db, context, batch, claims, found)
        finally:
            event.remove(Engine, "before_cursor_execute", observed)
            if not found:
                empty_queries.append(queries)

    monkeypatch.setattr(cover_sharing, "_publish", observed_publish)
    wire[1].extend(
        [
            {"list": [image(0), image(1)]},
            page([]),
            page([]),
            {"failed_infos": {}},
            page([image(0, target_id=True), image(1, target_id=True)]),
            page([image(0, target_id=True), image(1, target_id=True)]),
        ]
    )
    drive(source_env, redis_client, identities[0])
    drive(source_env, redis_client, identities[0], read=True)
    assert all(job_state(identity).status == "READY" for identity in identities)
    assert sum(call[0] == "POST" for call in wire[0]) == 1
    assert empty_queries and all(not queries for queries in empty_queries), [
        len(queries) for queries in empty_queries
    ]

    events = [
        json.loads(record.message.split(" ", 1)[1])
        for record in caplog.records
        if record.message.startswith("cover_share_slice ")
    ]
    assert events and events[-1]["outcome"] == "ready"
    assert all(
        item["tenant_id"] == str(source_env["context"].tenant_id) for item in events
    )
    assert any(item["outcome"] == "verification_queued" for item in events)
    assert all(item["elapsed_seconds"] >= item["check_seconds"] >= 0 for item in events)
    assert all(item["dispatch_id"] and item["revision"] >= 1 for item in events)


def test_share_fences_batch_twice_without_repeating_for_each_account(
    source_env, redis_client, wire, monkeypatch
):
    identities = matrix(source_env, 2, 10)
    checks_before_send = []
    check = cover_sharing._check

    def observed_check(db, context, batch, claims, **kwargs):
        if batch.armed_at and not any(call[0] == "POST" for call in wire[0]):
            checks_before_send.append(len(claims))
        return check(db, context, batch, claims, **kwargs)

    monkeypatch.setattr(cover_sharing, "_check", observed_check)
    wire[1].extend(
        [
            {"list": [image(0), image(1)]},
            *[page([]) for _ in range(10)],
            {"failed_infos": {}},
        ]
    )
    drive(source_env, redis_client, identities[0])
    assert sum(call[0] == "POST" for call in wire[0]) == 1
    # 准入前及实际发送前仍完整核验；逐账户检查不应再次重跑20成员的业务围栏。
    assert checks_before_send == [20, 20]
