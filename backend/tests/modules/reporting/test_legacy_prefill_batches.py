"""真实 PG 验证大页保全按批往返，且批次不改变同 CID 的观察顺序。"""

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from math import ceil
from time import perf_counter
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlmodel import Session

from app.modules.ads.creatives import upsert_creatives
from scripts.preserve_legacy_creative_identities import preserve_creative_identities
from tests.migration_database import historical_database
from tests.modules.reporting.sync_conftest import sync_seed


def _reference(db, tenant, ids, *, parent="parent", video="video", mid="mid"):
    db.execute(
        text("""INSERT INTO ad_material_reference
        (id,tenant_id,advertiser_id,ad_remote_id,platform_material_id,ad_material_id,
         material_type,name,creative_ids,complete,published_version)
        VALUES (:id,:tenant,'acc-1',:parent,:video,:mid,'VIDEO','legacy',CAST(:ids AS jsonb),true,1)
    """),
        {
            "id": uuid4(),
            "tenant": tenant,
            "ids": json.dumps(ids),
            "parent": parent,
            "video": video,
            "mid": mid,
        },
    )


@contextmanager
def _statements(engine):
    statements = []

    def record(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(" ".join(statement.split()))

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


@pytest.mark.parametrize("count", [128, 1024])
def test_large_single_parent_page_has_bounded_identity_reads_and_writes(
    monkeypatch, count, record_property
):
    with historical_database(monkeypatch, "0115_copy_current_source") as (
        engine,
        _config,
    ):
        with Session(engine) as db, db.begin():
            tenant = sync_seed.__wrapped__(db).tenant_id
            ids = [f"creative-{i:04}" for i in range(count)]
            _reference(db, tenant, ids)
        with Session(engine) as db, db.begin(), _statements(engine) as statements:
            started = perf_counter()
            result = preserve_creative_identities(
                db, tenant_id=tenant, advertiser_id="acc-1"
            )
        reads = [
            sql for sql in statements if sql.startswith("SELECT * FROM ad_creative")
        ]
        writes = [
            sql
            for sql in statements
            if sql.startswith(("INSERT INTO ad_creative", "UPDATE ad_creative"))
        ]
        record_property("identity_reads", len(reads))
        record_property("identity_writes", len(writes))
        record_property("sql_roundtrips", len(statements))
        record_property("elapsed_seconds", perf_counter() - started)
        assert len(reads) <= 4, (count, len(reads), len(statements))
        assert len(writes) <= 2 * ceil(count / 500), (count, len(writes))
        assert result["done"] and result["written"] >= count
        with Session(engine) as db, db.begin():
            records = (
                db.execute(
                    text("SELECT * FROM ad_creative WHERE tenant_id=:tenant"),
                    {"tenant": tenant},
                )
                .mappings()
                .all()
            )
            assert len(records) == count
            assert all(
                row["identity_status"] == "RESOLVED"
                and row["platform_material_id"] == "video"
                for row in records
            )
            assert all(
                len(row["evidence"]["legacy_identities"]) == 1 for row in records
            )
            assert (
                preserve_creative_identities(
                    db, tenant_id=tenant, advertiser_id="acc-1"
                )["written"]
                == 0
            )


def test_batch_enrichment_preserves_newer_state_and_every_conflict_proof(monkeypatch):
    with historical_database(monkeypatch, "0115_copy_current_source") as (
        engine,
        _config,
    ):
        now = datetime.now(UTC)
        with Session(engine) as db, db.begin():
            tenant = sync_seed.__wrapped__(db).tenant_id
            ids = [f"creative-{i:03}" for i in range(129)]
            _reference(db, tenant, ids)
            rows = [
                {
                    "creative_id": cid,
                    "ad_remote_id": "parent",
                    "ad_material_id": None,
                    "name": "live",
                    "operation_status": "DISABLE",
                    "deleted": True,
                    "assets": []
                    if i % 3 == 0
                    else [
                        {
                            "material_type": "VIDEO",
                            "platform_material_id": "video" if i % 3 == 1 else "other",
                        }
                    ],
                }
                for i, cid in enumerate(ids)
            ]
            upsert_creatives(
                db,
                tenant_id=tenant,
                advertiser_id="acc-1",
                rows=rows,
                observed_at=now,
                published_version=9,
            )
        with Session(engine) as db, db.begin():
            preserve_creative_identities(db, tenant_id=tenant, advertiser_id="acc-1")
            records = {
                r["creative_id"]: r
                for r in db.execute(
                    text("SELECT * FROM ad_creative WHERE tenant_id=:tenant"),
                    {"tenant": tenant},
                ).mappings()
            }
            for i, cid in enumerate(ids):
                row = records[cid]
                assert (
                    row["published_version"],
                    row["observed_at"],
                    row["name"],
                    row["operation_status"],
                    row["deleted"],
                ) == (9, now, "live", "DISABLE", True)
                if i % 3 == 2:
                    assert (
                        row["identity_status"] == "CONFLICT"
                        and row["platform_material_id"] == "other"
                    )
                    assert row["evidence"]["conflicts"][0]["published_version"] == 1
                    assert (
                        row["evidence"]["conflicts"][0]["identity"]["assets"][0][
                            "platform_material_id"
                        ]
                        == "video"
                    )
                else:
                    assert (
                        row["identity_status"] == "RESOLVED"
                        and row["platform_material_id"] == "video"
                    )
                    assert row["ad_material_id"] == "mid"
            assert (
                preserve_creative_identities(
                    db, tenant_id=tenant, advertiser_id="acc-1"
                )["written"]
                == 0
            )


def test_multiple_observations_of_one_cid_keep_parent_order_and_all_proofs(monkeypatch):
    with historical_database(monkeypatch, "0115_copy_current_source") as (
        engine,
        _config,
    ):
        with Session(engine) as db, db.begin():
            tenant = sync_seed.__wrapped__(db).tenant_id
            for parent, mid in [("a", None), ("a", "one"), ("b", "two")]:
                _reference(db, tenant, ["shared", "second"], parent=parent, mid=mid)
        with Session(engine) as db, db.begin():
            preserve_creative_identities(db, tenant_id=tenant, advertiser_id="acc-1")
            rows = (
                db.execute(
                    text("SELECT * FROM ad_creative WHERE tenant_id=:tenant"),
                    {"tenant": tenant},
                )
                .mappings()
                .all()
            )
            for row in rows:
                assert row["ad_remote_id"] == "a" and row["ad_material_id"] == "one"
                assert row["identity_status"] == "CONFLICT"
                assert (
                    row["evidence"]["conflicts"][0]["identity"]["ad_remote_id"] == "b"
                )
            assert (
                preserve_creative_identities(
                    db, tenant_id=tenant, advertiser_id="acc-1"
                )["written"]
                == 0
            )
