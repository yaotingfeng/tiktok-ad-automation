"""大目录应在单SQL期限内分批写，整次发布仍保持一个原子事务。"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.modules.ads import sync


def test_entity_writes_fit_statement_budget_and_rollback_all_batches(
    session, directory_seed, monkeypatch
):
    seed = directory_seed
    run = SimpleNamespace(
        tenant_id=seed.context.tenant_id,
        advertiser_id=seed.ref.advertiser_id,
        connection_id=seed.connection.id,
        channel="OFFICIAL_API",
        ad_type="REGULAR",
    )
    now = datetime.now(UTC)

    def staged(version):
        return sync._stage_entities(
            session,
            run,
            version,
            now + timedelta(seconds=version),
            [
                [
                    {
                        "ref": {
                            "tenant_id": str(run.tenant_id),
                            "advertiser_id": run.advertiser_id,
                            "kind": "campaign",
                            "remote_id": f"bounded-{i:04d}",
                        },
                        "ad_type": "REGULAR",
                        "name": f"version-{version}",
                        "configuration": {},
                    }
                    for i in range(600)
                ]
            ],
        )

    def publish(table):
        return [
            o
            for batch in sync._publish_staged_entities(session, run, table)
            for o in batch
        ]

    assert len(publish(staged(1))) == 600
    table = staged(2)
    suffix = uuid4().hex
    # 真正的PG行写耗时模拟生产大目录IO；不替身查询、触发器或事务。
    session.execute(
        text(f"""
        CREATE FUNCTION test_entity_delay_{suffix}() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.remote_id LIKE 'bounded-%' THEN
            IF current_setting('tkada.entity_abort',true)='on' AND NEW.remote_id='bounded-0200' THEN
              RAISE EXCEPTION 'bounded entity abort';
            END IF;
            PERFORM pg_sleep(0.001);
          END IF;
          RETURN NEW;
        END $$
    """)
    )
    session.execute(
        text(
            f"CREATE TRIGGER test_entity_delay_{suffix} BEFORE INSERT OR UPDATE ON ad_object FOR EACH ROW EXECUTE FUNCTION test_entity_delay_{suffix}()"
        )
    )
    monkeypatch.setattr(sync, "_ENTITY_WRITE_BATCH_SIZE", 32, raising=False)
    session.execute(text("SET LOCAL statement_timeout='600ms'"))
    with session.begin_nested():
        published = publish(table)
    assert len(published) == 600 and all(o.name == "version-2" for o in published)
    table = staged(3)
    session.execute(text("SET LOCAL tkada.entity_abort='on'"))
    with pytest.raises(DBAPIError, match="bounded entity abort"):
        with session.begin_nested():
            publish(table)
    session.execute(text("SET LOCAL tkada.entity_abort='off'"))
    values = session.execute(
        text(
            "SELECT DISTINCT name,published_version FROM ad_object WHERE tenant_id=:tenant AND advertiser_id=:advertiser AND remote_id LIKE 'bounded-%'"
        ),
        {"tenant": run.tenant_id, "advertiser": run.advertiser_id},
    ).all()
    assert values == [("version-2", 2)], "后批失败必须连同先前成功批次一起回滚"


def test_default_entity_quantum_covers_unsorted_large_stage_once(
    session, directory_seed
):
    seed = directory_seed
    run = SimpleNamespace(
        tenant_id=seed.context.tenant_id,
        advertiser_id=seed.ref.advertiser_id,
        connection_id=seed.connection.id,
        channel="OFFICIAL_API",
        ad_type="REGULAR",
    )
    # 超过生产5000条边界，输入倒序；正确性依赖暂存主键而不是网络页的顺序。
    expected = {f"large-boundary-{i:05d}" for i in range(6001)}
    items = [
        {
            "ref": {
                "tenant_id": str(run.tenant_id),
                "advertiser_id": run.advertiser_id,
                "kind": "campaign",
                "remote_id": identity,
            },
            "ad_type": "REGULAR",
            "name": "large boundary",
            "configuration": {},
        }
        for identity in sorted(expected, reverse=True)
    ]
    staged = sync._stage_entities(session, run, 7, datetime.now(UTC), [items])
    published = [
        o
        for batch in sync._publish_staged_entities(session, run, staged)
        for o in batch
    ]
    assert len(published) == len(expected)
    assert {o.remote_id for o in published} == expected
    assert all(
        o.published_version == 7 and o.name == "large boundary" for o in published
    )
