"""Maintenance cursor advances only after committed pages owned by this worker."""

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.core.db import engine
from app.modules.materials import cleanup_scan as scan


@pytest.fixture(autouse=True)
def isolated_scan_keys(monkeypatch, redis_client, redis_key_prefix):
    keys = [f"{redis_key_prefix}:scan-lock", f"{redis_key_prefix}:scan-cursor"]
    monkeypatch.setattr(scan, "LOCK_KEY", keys[0])
    monkeypatch.setattr(scan, "CURSOR_KEY", keys[1])
    try:
        yield
    finally:
        redis_client.delete(*keys)


def test_scan_cursor_advances_and_wraps_without_repeating_first_unsafe_page(
    monkeypatch, redis_client
):
    monkeypatch.setattr(settings, "MATERIAL_CLEANUP_ENABLED", True)
    seen = []

    def page(db, *, cursor, limit, enqueue):
        assert enqueue and limit == 100
        assert db.execute(text("SELECT 1")).scalar_one() == 1
        seen.append(cursor)
        return {
            "next_cursor": {None: "page-2", "page-2": "page-3", "page-3": None}[cursor],
            "queued": 1,
        }

    monkeypatch.setattr(scan, "scan_abandoned_objects", page)
    for _ in range(3):
        assert (
            scan.scan_abandonment_page(
                database_engine=engine, redis_client=redis_client
            )
            == 1
        )
    assert seen == [None, "page-2", "page-3"]
    assert redis_client.get(scan.CURSOR_KEY) is None
    assert redis_client.get(scan.LOCK_KEY) is None


def test_failed_page_and_expired_owner_cannot_advance_or_erase_new_lock(
    monkeypatch, redis_client
):
    monkeypatch.setattr(settings, "MATERIAL_CLEANUP_ENABLED", True)
    redis_client.set(scan.CURSOR_KEY, "before")

    def failed(_db, **_kwargs):
        raise RuntimeError("simulated transaction failure")

    monkeypatch.setattr(scan, "scan_abandoned_objects", failed)
    with pytest.raises(RuntimeError):
        scan.scan_abandonment_page(database_engine=engine, redis_client=redis_client)
    assert redis_client.get(scan.CURSOR_KEY) == "before"
    assert redis_client.get(scan.LOCK_KEY) is None

    def lost_owner(_db, **_kwargs):
        redis_client.set(scan.LOCK_KEY, "next-owner", ex=75)
        return {"next_cursor": "stale-page", "queued": 0}

    monkeypatch.setattr(scan, "scan_abandoned_objects", lost_owner)
    scan.scan_abandonment_page(database_engine=engine, redis_client=redis_client)
    assert redis_client.get(scan.CURSOR_KEY) == "before"
    assert redis_client.get(scan.LOCK_KEY) == "next-owner"


def test_disabled_cleanup_does_not_open_a_scan(monkeypatch):
    monkeypatch.setattr(settings, "MATERIAL_CLEANUP_ENABLED", False)
    assert scan.scan_abandonment_page(database_engine=engine, redis_client=None) == 0
