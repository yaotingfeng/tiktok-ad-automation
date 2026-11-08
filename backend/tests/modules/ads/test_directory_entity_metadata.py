"""真实PG验证元数据访问只随本段键增长，不重写宽payload暂存。"""

import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace

from sqlalchemy import text

from app.modules.ads import sync


def test_metadata_does_not_rescan_or_rewrite_wide_payload_heap(session, directory_seed):
    seed = directory_seed
    run = SimpleNamespace(
        tenant_id=seed.context.tenant_id,
        advertiser_id=seed.ref.advertiser_id,
        connection_id=seed.connection.id,
        channel="OFFICIAL_API",
        ad_type="REGULAR",
        kind="creative",
    )
    items = [
        {
            "ref": {
                "tenant_id": str(run.tenant_id),
                "advertiser_id": run.advertiser_id,
                "kind": "creative",
                "remote_id": f"metadata-physical-{i:05d}",
            },
            "ad_type": "REGULAR",
            "configuration": {
                "wide": "".join(
                    hashlib.sha256(f"{i}:{part}".encode()).hexdigest()
                    for part in range(20)
                )
            },
        }
        for i in reversed(range(6001))
    ]
    staged = sync._stage_entities(
        session, run, 17, datetime.now(UTC), sync._batches(items)
    )
    # 线上已有完整目录及统计；空测试表的旧统计会把任意账户索引误估成单行。
    # 先建实际旧对象并ANALYZE，再观察本次批量发布的真实物理访问。
    session.execute(
        text(f"""
        INSERT INTO ad_object
            (tenant_id,advertiser_id,kind,remote_id,ad_type,name,configuration,
             observed_at,published_version)
        SELECT :tenant,:advertiser,kind,remote_id,'REGULAR','old','{{}}'::jsonb,
            now()-interval '1 day',1 FROM {staged}
    """),
        {"tenant": run.tenant_id, "advertiser": run.advertiser_id},
    )
    session.execute(text("ANALYZE ad_object"))
    before = dict(
        session.execute(text(f"SELECT remote_id,ctid::text FROM {staged}")).all()
    )
    raw = session.connection().connection.driver_connection
    plans = []

    def capture(diag):
        message = diag.message_primary or ""
        if "plan:" in message and "{" in message:
            plans.append(json.loads(message[message.index("{") :]))

    session.execute(text("LOAD 'auto_explain'"))
    for setting, value in {
        "client_min_messages": "log",
        "auto_explain.log_min_duration": "0",
        "auto_explain.log_analyze": "on",
        "auto_explain.log_buffers": "on",
        "auto_explain.log_timing": "off",
        "auto_explain.log_nested_statements": "off",
        "auto_explain.log_format": "json",
    }.items():
        session.execute(text(f"SET LOCAL {setting}='{value}'"))
    raw.add_notice_handler(capture)
    try:
        published = [
            obj
            for batch in sync._publish_staged_entities(session, run, staged)
            for obj in batch
        ]
    finally:
        raw.remove_notice_handler(capture)
    assert len(published) == 6001
    assert all(obj.published_version == 17 for obj in published)

    # 以实际ModifyTable/正式对象PK子计划识别元数据回写，不固定SQL文本模板。
    def nodes(node):
        yield node
        for child in node.get("Plans", []):
            yield from nodes(child)

    metadata = [
        p["Plan"]
        for p in plans
        if p["Plan"]["Node Type"] == "ModifyTable"
        and p["Plan"].get("Relation Name") != "ad_object"
        and any(n.get("Relation Name") == "ad_object" for n in nodes(p["Plan"]))
    ]
    assert len(metadata) == 2
    touched = sum(
        (n.get("Actual Rows", 0) + n.get("Rows Removed by Filter", 0))
        * n.get("Actual Loops", 1)
        for plan in metadata
        for n in nodes(plan)
        if n.get("Relation Name") == staged and "Scan" in n["Node Type"]
    )
    assert touched == 0, f"metadata revisited {touched} wide payload rows"
    for plan, expected in zip(metadata, (5000, 1001), strict=True):
        lookups = [n for n in nodes(plan) if n.get("Relation Name") == "ad_object"]
        assert len(lookups) == 1 and lookups[0]["Index Name"] == "ad_object_pkey"
        assert lookups[0]["Actual Rows"] == 1
        assert lookups[0]["Actual Loops"] == expected
    after = dict(
        session.execute(text(f"SELECT remote_id,ctid::text FROM {staged}")).all()
    )
    assert after == before, "metadata must not create new versions of wide temp tuples"
    owners = sync._material_owner_objects(
        session,
        run,
        [{"creative_ids": ["metadata-physical-00000", "metadata-physical-06000"]}],
        staged,
    )
    assert len(owners) == 2 and all(row.published_version == 17 for row in owners)


def test_metadata_failure_in_later_batch_rolls_back_prior_entities_and_owners(
    session, directory_seed, monkeypatch
):
    from uuid import uuid4

    import pytest
    from sqlalchemy.exc import DBAPIError

    seed = directory_seed
    run = SimpleNamespace(
        tenant_id=seed.context.tenant_id,
        advertiser_id=seed.ref.advertiser_id,
        connection_id=seed.connection.id,
        channel="OFFICIAL_API",
        ad_type="REGULAR",
        kind="creative",
    )
    items = [
        {
            "ref": {
                "tenant_id": str(run.tenant_id),
                "advertiser_id": run.advertiser_id,
                "kind": "creative",
                "remote_id": f"metadata-abort-{i:03d}",
            },
            "configuration": {},
        }
        for i in range(80)
    ]
    staged = sync._stage_entities(session, run, 3, datetime.now(UTC), [items])
    metadata = sync._entity_metadata_table(staged)
    suffix = uuid4().hex
    session.execute(
        text(f"""
        CREATE FUNCTION metadata_abort_{suffix}() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.remote_id='metadata-abort-065' THEN
            RAISE EXCEPTION 'later metadata abort';
          END IF;
          RETURN NEW;
        END $$
    """)
    )
    session.execute(
        text(
            f"CREATE TRIGGER metadata_abort_{suffix} BEFORE INSERT ON {metadata} FOR EACH ROW EXECUTE FUNCTION metadata_abort_{suffix}()"
        )
    )
    monkeypatch.setattr(sync, "_ENTITY_WRITE_BATCH_SIZE", 32)
    with pytest.raises(DBAPIError, match="later metadata abort"):
        with session.begin_nested():
            list(sync._publish_staged_entities(session, run, staged))
    assert session.execute(text(f"SELECT count(*) FROM {metadata}")).scalar_one() == 0
    assert (
        session.execute(
            text(
                "SELECT count(*) FROM ad_object WHERE tenant_id=:tenant AND advertiser_id=:advertiser AND remote_id LIKE 'metadata-abort-%'"
            ),
            {"tenant": run.tenant_id, "advertiser": run.advertiser_id},
        ).scalar_one()
        == 0
    )
    assert session.execute(text(f"SELECT count(*) FROM {staged}")).scalar_one() == 80


# 独立提交夹具提供真实并发事务，不能用同连接savepoint模拟READ COMMITTED快照。
from tests.modules.reporting.sync_conftest import (  # noqa: E402
    committed_sync_seed as committed_sync_seed,
)


def test_metadata_reads_concurrent_winner_after_upsert_snapshot(
    committed_sync_seed,
):
    from concurrent.futures import ThreadPoolExecutor
    from datetime import timedelta
    from queue import Queue
    from time import monotonic, sleep

    from app.core.db import engine
    from app.modules.ads.models import AdObject

    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    run = SimpleNamespace(
        tenant_id=seed.tenant_id,
        advertiser_id="acc-1",
        connection_id=seed.connection.id,
        channel="OFFICIAL_API",
        ad_type="REGULAR",
        kind="creative",
    )
    identity = (seed.tenant_id, "acc-1", "creative", "metadata-racing-winner")
    with factory() as initial:
        initial.add(
            AdObject(
                tenant_id=seed.tenant_id,
                advertiser_id="acc-1",
                kind="creative",
                remote_id=identity[-1],
                parent_kind="ad",
                parent_remote_id="old-parent",
                ad_type="REGULAR",
                observed_at=now - timedelta(hours=1),
                published_version=1,
            )
        )
        initial.commit()
    ready = Queue()

    def losing_publication():
        with factory() as publishing:
            assert (
                publishing.execute(text("SHOW transaction_isolation")).scalar_one()
                == "read committed"
            )
            publishing.execute(text("SET LOCAL statement_timeout='5s'"))
            staged = sync._stage_entities(
                publishing,
                run,
                8,
                now,
                [
                    [
                        {
                            "ref": {
                                "tenant_id": str(seed.tenant_id),
                                "advertiser_id": "acc-1",
                                "kind": "creative",
                                "remote_id": identity[-1],
                            },
                            "parent_ref": {
                                "tenant_id": str(seed.tenant_id),
                                "advertiser_id": "acc-1",
                                "kind": "ad",
                                "remote_id": "losing-parent",
                            },
                            "configuration": {},
                        }
                    ]
                ],
            )
            ready.put(publishing.execute(text("SELECT pg_backend_pid()")).scalar_one())
            list(sync._publish_staged_entities(publishing, run, staged))
            owners = sync._material_owner_objects(
                publishing, run, [{"creative_ids": [identity[-1]]}], staged
            )
            assert len(owners) == 1
            foreign = SimpleNamespace(**(vars(run) | {"advertiser_id": "acc-2"}))
            assert (
                sync._material_owner_objects(
                    publishing, foreign, [{"creative_ids": [identity[-1]]}], staged
                )
                == []
            )
            return owners[0].published_version, owners[0].parent_remote_id

    with factory() as winner, ThreadPoolExecutor(max_workers=1) as pool:
        winning = winner.get(AdObject, identity, with_for_update=True)
        winning.parent_remote_id = "committed-winner-parent"
        winning.observed_at = now + timedelta(hours=1)
        winning.published_version = 9
        winner.flush()
        blocker_pid = winner.execute(text("SELECT pg_backend_pid()")).scalar_one()
        future = pool.submit(losing_publication)
        waiting_pid = ready.get(timeout=5)
        blocked = False
        try:
            deadline = monotonic() + 3
            with engine.connect().execution_options(
                isolation_level="AUTOCOMMIT"
            ) as check:
                while monotonic() < deadline:
                    if (
                        blocker_pid
                        in check.execute(
                            text("SELECT pg_blocking_pids(:pid)"), {"pid": waiting_pid}
                        ).scalar_one()
                    ):
                        blocked = True
                        break
                    sleep(0.01)
        finally:
            # 真实UPSERT已使用旧快照并阻塞；随后提交更晚观察，再验证新语句赢家。
            winner.commit()
        assert blocked, (
            "must observe the actual upsert waiting on the concurrent winner"
        )
        assert future.result(timeout=5) == (9, "committed-winner-parent")
