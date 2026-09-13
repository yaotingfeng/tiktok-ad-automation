"""Preview inspects local evidence and configuration; no network or outbox writes."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, select

from app.core.config import settings
from app.core.db import engine
from app.jobs.models import PendingDispatch
from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess
from app.modules.materials.models import AccountMaterial, MaterialFile
from app.modules.materials.readiness import choose_material_path, get_material_readiness
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire


def target(
    session, env, *, advertiser_id="target-account", can_upload=True, state="VERIFIED"
):
    session.add(
        AdvertiserAccount(
            tenant_id=env["context"].tenant_id,
            advertiser_id=advertiser_id,
            currency="USD",
            timezone="UTC",
            remote_status="ENABLE",
        )
    )
    session.flush()
    session.add(
        BCAccountAccess(
            tenant_id=env["context"].tenant_id,
            bc_id=env["bc_id"],
            advertiser_id=advertiser_id,
            connection_id=env["connection_id"],
            in_bc=True,
            active=True,
            authorized=True,
            can_build=True,
            can_upload=can_upload,
            permission_state=state,
            checked_at=datetime.now(UTC),
        )
    )
    session.flush()
    return advertiser_id


def asset(
    session, env, account, *, seconds_old=0, status="available", mid="source-mid"
):
    row = AccountMaterial(
        tenant_id=env["context"].tenant_id,
        bc_id=env["bc_id"],
        material_id=env["material_id"],
        advertiser_id=account,
        connection_id=env["connection_id"],
        video_id=f"vid-{account}",
        mid=mid,
        status=status,
        verified_at=datetime.now(UTC) - timedelta(seconds=seconds_old),
    )
    session.add(row)
    session.flush()
    return row


@pytest.fixture(autouse=True)
def runtime_config(monkeypatch):
    monkeypatch.setattr(settings, "S3_BUCKET", "offline-fixtures")
    monkeypatch.setattr(settings, "S3_ACCESS_KEY_ID", "offline-key")
    monkeypatch.setattr(settings, "S3_SECRET_ACCESS_KEY", "offline-secret")


def read(env, account):
    with Session(engine) as session:
        return get_material_readiness(
            session,
            context=env["context"],
            bc_id=env["bc_id"],
            material_id=env["material_id"],
            advertiser_id=account,
        )


def test_path_choice_preserves_original_after_source_loss():
    assert choose_material_path(
        target_verified=False,
        target_known=False,
        shareable_source=False,
        original_available=True,
    ) == ("preparable", "upload_original")
    assert choose_material_path(
        target_verified=False,
        target_known=False,
        shareable_source=False,
        original_available=False,
    ) == ("blocked", "unavailable")


def test_preview_is_read_only_and_fresh_target_requires_current_permission(
    source_env, wire, monkeypatch
):
    with Session(engine) as session, session.begin():
        account = target(session, source_env)
        expected = asset(session, source_env, account)
        expected_id = expected.id
    monkeypatch.setattr(
        "app.jobs.outbox.enqueue_after_commit",
        lambda *_a, **_kw: pytest.fail("preview queued a write"),
    )
    result = read(source_env, account)
    assert result.state == "ready" and result.mapping.asset_id == expected_id
    with Session(engine) as session, session.begin():
        session.get(
            BCAccountAccess,
            (
                source_env["context"].tenant_id,
                source_env["bc_id"],
                account,
                source_env["connection_id"],
            ),
        ).permission_state = "UNKNOWN"
    assert read(source_env, account).state == "blocked"
    assert wire[0] == []
    with Session(engine) as session:
        assert (
            session.exec(
                select(PendingDispatch).where(
                    PendingDispatch.tenant_id == source_env["context"].tenant_id
                )
            ).all()
            == []
        )


def test_expired_target_is_preparable_for_readback(source_env, wire):
    with Session(engine) as session, session.begin():
        account = target(session, source_env)
        asset(session, source_env, account, seconds_old=901)
    result = read(source_env, account)
    assert (result.state, result.path) == ("preparable", "existing_target")
    assert wire[0] == []


def test_authorized_same_bc_source_uses_native_share_without_original(
    source_env, wire
):
    with Session(engine) as session, session.begin():
        account = target(session, source_env)
        asset(session, source_env, "actual-account")
    result = read(source_env, account)
    assert (result.state, result.path) == ("preparable", "share_source")
    with Session(engine) as session, session.begin():
        session.get(
            MaterialFile, source_env["material_id"]
        ).storage_state = "unavailable"
    result = read(source_env, account)
    assert (
        result.state == "preparable" and result.path == "share_source"
    )
    assert wire[0] == []


def test_all_sources_are_checked_beyond_first_representative(source_env, wire):
    with Session(engine) as session, session.begin():
        destination = target(session, source_env)
        account2 = target(session, source_env, advertiser_id="second-source")
        asset(session, source_env, "actual-account", mid=None)
        asset(session, source_env, account2, mid="valid-mid")
        session.get(
            MaterialFile, source_env["material_id"]
        ).storage_state = "unavailable"
    result = read(source_env, destination)
    assert result.state == "preparable" and result.path == "share_source"
    assert wire[0] == []


def test_known_upload_capacity_and_configuration_failures_are_blocked(
    source_env, wire, monkeypatch
):
    with Session(engine) as session, session.begin():
        account = target(session, source_env)
        session.get(MaterialFile, source_env["material_id"]).byte_size = (
            settings.MATERIAL_SDK_MAX_UPLOAD_BYTES + 1
        )
    assert read(source_env, account).reason_code == "sdk_upload_capacity_exceeded"
    with Session(engine) as session, session.begin():
        session.get(MaterialFile, source_env["material_id"]).byte_size = 20
    monkeypatch.setattr(settings, "TIKTOK_CALL_POLICIES", {})
    assert read(source_env, account).reason_code == "admission_unconfigured"
    assert wire[0] == []


def test_no_upload_capability_and_unknown_permissions_block_target(source_env, wire):
    with Session(engine) as session, session.begin():
        account = target(session, source_env, can_upload=False)
    assert read(source_env, account).state == "blocked"
    assert wire[0] == []


def test_preparable_preview_runs_in_postgresql_read_only_transaction(source_env, wire):
    from sqlalchemy import text

    with Session(engine) as session, session.begin():
        account = target(session, source_env)
    with Session(engine) as session, session.begin():
        session.execute(text("SET TRANSACTION READ ONLY"))
        result = get_material_readiness(
            session,
            context=source_env["context"],
            bc_id=source_env["bc_id"],
            material_id=source_env["material_id"],
            advertiser_id=account,
        )
        assert result.state == "preparable" and not session.new and not session.dirty
    assert wire[0] == []


def test_cache_window_is_configured_and_ready_assets_do_not_need_upload_capacity(
    source_env, wire, monkeypatch
):
    with Session(engine) as session, session.begin():
        account = target(session, source_env)
        asset(session, source_env, account, seconds_old=120)
        session.get(MaterialFile, source_env["material_id"]).byte_size = (
            settings.MATERIAL_SDK_MAX_UPLOAD_BYTES + 1
        )
    monkeypatch.setattr(settings, "MATERIAL_ASSET_MAX_AGE_SECONDS", 180)
    assert read(source_env, account).state == "ready"
    monkeypatch.setattr(settings, "MATERIAL_ASSET_MAX_AGE_SECONDS", 60)
    result = read(source_env, account)
    assert (result.state, result.path) == ("preparable", "existing_target")
    assert wire[0] == []
