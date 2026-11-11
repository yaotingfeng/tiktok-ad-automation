"""无本地素材/搭建关联的来源，用真实PG及平台传输边界替身准备。"""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlmodel import Session, select

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.builds import CtaCreate
from app.integrations.tiktok.contracts.common import CallEvidence
from app.integrations.tiktok.contracts.materials import ImageRecord, VideoRecord
from app.modules.builds import copying, previews
from app.modules.campaign_copies import source_facts
from app.modules.campaign_copies.models import CopySourceFacts
from app.modules.materials.cover_models import MaterialCoverJob
from app.modules.materials.models import AccountMaterial, MaterialFile
from tests.modules.builds.test_campaign_copy_flow import (
    complete_offline_catalog as complete_offline_catalog,
)
from tests.modules.builds.test_campaign_copy_flow import copy_source as copy_source
from tests.modules.builds.test_campaign_copy_flow import (
    external_source,
)
from tests.modules.builds.test_execution import executable as executable


def queue_facts(session, context, source):
    with pytest.raises(DomainError) as error:
        source_facts.ensure_source_facts(session, context=context, source=source)
    assert error.value.code == "copy_preparing"
    session.flush()
    return (
        session.exec(
            select(CopySourceFacts).where(
                CopySourceFacts.source_digest == source_facts.source_key(source)
            )
        )
        .one()
        .id
    )


def transport(
    monkeypatch,
    calls,
    *,
    bad_video=False,
    during_read=None,
    video_displayable=True,
    image_displayable=True,
):
    class MaterialRead:
        def read_video(self, *, advertiser_id, video_id, budget):
            calls.append(("video", advertiser_id, video_id))
            if during_read:
                during_read()
            return VideoRecord(
                advertiser_id=advertiser_id,
                video_id="wrong-video" if bad_video else video_id,
                mid="video-mid",
                md5="a" * 32,
                file_name="platform.mp4",
                width=720,
                height=1280,
                size=12345,
                duration=30.0,
                format="mp4",
                displayable=video_displayable,
                evidence=CallEvidence(),
            )

        def read_images(self, *, advertiser_id, image_ids, budget):
            calls.append(("image", advertiser_id, image_ids))
            return (
                ImageRecord(
                    advertiser_id=advertiser_id,
                    image_id=image_ids[0],
                    signature="b" * 32,
                    file_name="cover",
                    width=720,
                    height=1280,
                    displayable=image_displayable,
                    evidence=CallEvidence(),
                    mid="image-mid",
                ),
            )

    class BuildRead:
        def read_cta_portfolio(self, *, advertiser_id, portfolio_id):
            calls.append(("cta", advertiser_id, portfolio_id))
            return CtaCreate(
                advertiser_id=advertiser_id,
                assets=({"asset_ids": ["cta-1"], "asset_content": "Watch now"},),
            )

    @contextmanager
    def gateway(**_kwargs):
        yield SimpleNamespace(materials=MaterialRead(), builds=BuildRead())

    monkeypatch.setattr(source_facts, "open_tiktok_gateway", gateway)


def step(engine, context, identity):
    with Session(engine) as session:
        row = session.get(CopySourceFacts, identity)
        payload = {"facts_id": str(identity), "generation": row.generation}
    source_facts.process_source(
        database_engine=engine,
        redis_client=object(),
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        payload=payload,
    )
    return payload


def test_external_material_is_automatically_prepared_and_preview_is_submittable(
    copy_source, monkeypatch
):
    engine, context, _, configuration = copy_source
    calls = []
    transport(monkeypatch, calls)
    with Session(engine) as session, session.begin():
        source = external_source(session, context, remote_video=True)
        identity = queue_facts(session, context, source)
        mid = UUID(source["materials"][0]["material_id"])
        assert session.get(MaterialFile, mid) is None
    first = step(engine, context, identity)
    # 过期同代数重投只读结果，不再发平台请求。
    source_facts.process_source(
        database_engine=engine,
        redis_client=object(),
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        payload=first,
    )
    step(engine, context, identity)
    assert [c[0] for c in calls] == ["video", "image", "cta"]
    with Session(engine) as session, session.begin():
        row = session.get(CopySourceFacts, identity)
        assert row.status == "READY"
        file = session.get(MaterialFile, mid)
        assert file.byte_size == 12345 and file.video_md5 == "a" * 32
        assert file.storage_state == "unavailable" and file.sha256 is None
        asset = session.exec(
            select(AccountMaterial).where(AccountMaterial.material_id == mid)
        ).one()
        assert asset.video_id == "external-video" and asset.image_id == "external-cover"
        cover = session.exec(
            select(MaterialCoverJob).where(MaterialCoverJob.material_id == mid)
        ).one()
        assert (
            cover.purpose == "SOURCE" and cover.candidate_image_id == "external-cover"
        )
        assert cover.image_mid == "image-mid"
        configuration = {**configuration, "material_ids": [str(mid)]}
        preview_id = copying.create_copy_preview(
            session,
            context=context,
            bc_id="bc-draft",
            source=source,
            configuration=configuration,
            advertiser_id="account-A",
            ordinal=1,
            request_id=uuid4(),
        )
        assert (
            previews.get_preview_summary(
                session, context=context, preview_id=preview_id
            ).ad_count
            > 0
        )


def test_wrong_remote_video_never_publishes_a_local_mapping(copy_source, monkeypatch):
    engine, context, _, _ = copy_source
    calls = []
    transport(monkeypatch, calls, bad_video=True)
    with Session(engine) as session, session.begin():
        source = external_source(session, context, remote_video=True)
        identity = queue_facts(session, context, source)
        mid = UUID(source["materials"][0]["material_id"])
    step(engine, context, identity)
    with Session(engine) as session:
        row = session.get(CopySourceFacts, identity)
        assert row.status == "FAILED" and row.error_code == "copy_material_unverified"
        assert session.get(MaterialFile, mid) is None


def test_expired_read_claim_is_repaired_without_reusing_old_generation(copy_source):
    engine, context, _, _ = copy_source
    with Session(engine) as session, session.begin():
        source = external_source(session, context, remote_video=True)
        identity = queue_facts(session, context, source)
        row = session.get(CopySourceFacts, identity)
        generation = row.generation
        row.claim_token = uuid4()
        row.claimed_until = datetime.now(UTC) - timedelta(seconds=1)
        row.repair_after = datetime.now(UTC) - timedelta(seconds=1)
        session.add(row)
    with Session(engine) as session, session.begin():
        assert source_facts.repair_sources(session) == 1
        row = session.get(CopySourceFacts, identity)
        assert row.generation == generation + 1 and row.claim_token is None


@pytest.mark.parametrize(
    "video_flag,image_flag,code",
    [
        (False, True, "copy_material_unverified"),
        (None, True, "copy_material_unverified"),
        (True, False, "copy_cover_unverified"),
        (True, None, "copy_cover_unverified"),
    ],
)
def test_unavailable_or_unknown_assets_never_become_available(
    copy_source, monkeypatch, video_flag, image_flag, code
):
    engine, context, _, _ = copy_source
    calls = []
    transport(
        monkeypatch, calls, video_displayable=video_flag, image_displayable=image_flag
    )
    with Session(engine) as session, session.begin():
        source = external_source(session, context, remote_video=True)
        identity = queue_facts(session, context, source)
        mid = UUID(source["materials"][0]["material_id"])
    step(engine, context, identity)
    with Session(engine) as session:
        assert session.get(CopySourceFacts, identity).error_code == code
        assert session.get(MaterialFile, mid) is None


def test_explicit_retry_restarts_a_failed_read_and_completes(copy_source, monkeypatch):
    engine, context, _, _ = copy_source
    calls = []
    transport(monkeypatch, calls, bad_video=True)
    with Session(engine) as session, session.begin():
        source = external_source(session, context, remote_video=True)
        identity = queue_facts(session, context, source)
    step(engine, context, identity)
    with Session(engine) as session, session.begin():
        assert session.get(CopySourceFacts, identity).status == "FAILED"
        source_facts.retry_failed_source(session, context=context, source=source)
        assert session.get(CopySourceFacts, identity).status == "PENDING"
    transport(monkeypatch, calls)
    step(engine, context, identity)
    step(engine, context, identity)
    with Session(engine) as session:
        assert session.get(CopySourceFacts, identity).status == "READY"


def test_quota_wait_does_not_consume_source_read_failure_budget(
    copy_source, monkeypatch
):
    from app.integrations.tiktok.sdk import AccountAdmissionDeferred

    engine, context, _, _ = copy_source

    @contextmanager
    def waiting(**_kwargs):
        raise AccountAdmissionDeferred(300000)
        yield

    monkeypatch.setattr(source_facts, "open_tiktok_gateway", waiting)
    with Session(engine) as session, session.begin():
        source = external_source(session, context, remote_video=True)
        identity = queue_facts(session, context, source)
    for _ in range(12):
        step(engine, context, identity)
    with Session(engine) as session:
        row = session.get(CopySourceFacts, identity)
        assert row.status == "PENDING" and row.failures == 0


def test_concurrent_series_reuse_one_material_registration(copy_source):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from time import monotonic, sleep

    from sqlalchemy import text

    from app.integrations.tiktok.contracts.context import FrozenTikTokRoute

    engine, context, _, _ = copy_source
    with Session(engine) as session, session.begin():
        source = external_source(session, context, remote_video=True)
    item = {**source["materials"][0], "advertiser_id": "account-A"}
    route = FrozenTikTokRoute.model_validate(source["snapshot"]["source_route"])
    video = VideoRecord(
        advertiser_id="account-A",
        video_id="external-video",
        mid="video-mid",
        md5="a" * 32,
        file_name="v.mp4",
        width=720,
        height=1280,
        size=12345,
        duration=30.0,
        format="mp4",
        displayable=True,
        evidence=CallEvidence(),
    )
    image = ImageRecord(
        advertiser_id="account-A",
        image_id="external-cover",
        signature="b" * 32,
        file_name="cover",
        width=720,
        height=1280,
        displayable=True,
        evidence=CallEvidence(),
        mid="image-mid",
    )
    started = Event()
    pid = []

    def register_second():
        with Session(engine) as session, session.begin():
            session.execute(text("SET LOCAL lock_timeout='5s'"))
            pid.append(session.execute(text("SELECT pg_backend_pid()")).scalar_one())
            started.set()
            source_facts._record_video(
                session,
                context=context,
                route=route,
                item=item,
                record=video,
                image=image,
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        with Session(engine) as first, first.begin():
            source_facts._record_video(
                first,
                context=context,
                route=route,
                item=item,
                record=video,
                image=image,
            )
            second = pool.submit(register_second)
            assert started.wait(5)
            deadline = monotonic() + 4
            blocked = False
            while monotonic() < deadline:
                with engine.connect() as probe:
                    blocked = probe.execute(
                        text("SELECT cardinality(pg_blocking_pids(:pid))>0"),
                        {"pid": pid[0]},
                    ).scalar_one()
                if blocked:
                    break
                sleep(0.02)
            assert blocked
        second.result(timeout=6)
    with Session(engine) as session:
        mid = UUID(item["material_id"])
        assert (
            len(
                session.exec(
                    select(AccountMaterial).where(AccountMaterial.material_id == mid)
                ).all()
            )
            == 1
        )
        assert (
            len(
                session.exec(
                    select(MaterialCoverJob).where(MaterialCoverJob.material_id == mid)
                ).all()
            )
            == 1
        )
