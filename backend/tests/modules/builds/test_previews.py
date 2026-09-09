from decimal import Decimal
from itertools import islice
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlmodel import select

from app.modules.builds import previews
from app.modules.builds.drafts import create_draft, edit_material_groups, prepare_draft
from app.modules.builds.models import DraftDrama
from app.modules.builds.previews import iter_pairs, unit_readiness
from app.modules.builds.scene_schemas import SceneContext
from app.modules.strategies.models import StrategyVersion
from app.modules.strategies.service import append_version
from tests.modules.builds.test_drafts import account, finish, ready_links
from tests.modules.materials.test_tenant_materials import material
from tests.modules.strategies.test_versions import config


def test_preview_product_is_lazy_and_covers_same_accounts_for_every_drama():
    pairs = iter_pairs(range(100_000), lambda: iter(("a", "b", "c")))
    assert list(islice(pairs, 7)) == [
        (0, "a"),
        (0, "b"),
        (0, "c"),
        (1, "a"),
        (1, "b"),
        (1, "c"),
        (2, "a"),
    ]
    assert len(list(iter_pairs(range(2), lambda: iter(("a", "b", "c"))))) == 6


def test_preview_readiness_requires_scene_currency_and_every_material_path():
    assert unit_readiness("USD", "USD", ["ready", "preparable"], []) == "PREPARING"
    assert unit_readiness("USD", "USD", ["ready"], []) == "READY"
    assert unit_readiness("USD", "EUR", ["ready"], []) == "BLOCKED"
    assert unit_readiness("USD", "USD", ["ready", "blocked"], []) == "BLOCKED"
    assert unit_readiness("USD", "USD", ["ready"], ["minis_unavailable"]) == "BLOCKED"
    assert unit_readiness("USD", "USD", [], []) == "BLOCKED"


@pytest.fixture
def prepared(session, context, intent, monkeypatch):
    version = session.get(StrategyVersion, intent["strategy_version_id"])
    intent = dict(
        intent,
        strategy_version_id=append_version(
            session,
            context=context,
            strategy_id=version.strategy_id,
            config=config(budget="100", creative_count=2),
        ),
        drama_lines=["Moon", "Short Drama"],
        account_lines=["A", "B", "C"],
    )
    for name in intent["account_lines"]:
        account(session, context, name)
    for title in intent["drama_lines"]:
        for i in range(23):
            material(session, context, f"{title} {i:03}.mp4", bc="bc-draft")
    draft = create_draft(session, context=context, **intent)
    task = prepare_draft(session, context=context, draft_id=draft, request_id=uuid4())
    ready_links(session, context, task, intent)
    finish(session, context, task)
    scene = SceneContext(
        supported=True,
        reason_codes=(),
        capability_revision="fixture-v1",
        name_limit=512,
        creative_limit=50,
        copy_length_limit=100,
        field_constraints={
            "name_limits": {"campaign": 512, "adgroup": 512, "ad": 512},
            "name_measurement": {
                "campaign": "cjk_weighted",
                "adgroup": "characters",
                "ad": "cjk_weighted",
            },
            "copy_measurement": "characters",
            "max_ads_per_adgroup": 30,
            "roas_bid": {"minimum": "0.01", "maximum": "1000"},
            "campaign_daily_budget": {
                "currency": "USD",
                "minimum_inclusive": "50",
                "maximum_exclusive": "10000000",
                "precision": "0.01",
            },
        },
        cta_fields={"asset_ids": ["cta-1"], "requires_portfolio_creation": True},
    )
    monkeypatch.setattr(previews, "read_scene_context", lambda *a, **kw: scene)
    monkeypatch.setattr(
        previews,
        "get_material_readiness_batch",
        lambda *a, **kw: {
            identity: SimpleNamespace(state="preparable", reason_code=None)
            for identity in kw["material_ids"]
        },
    )
    return draft


def drain(session, context, identity):
    for _ in range(200):
        if previews.continue_preview(session, context=context, preview_id=identity):
            return
    raise AssertionError("preview never finished")


def test_frozen_full_product_budget_and_shared_copy(session, context, prepared):
    identity = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    assert (
        previews.generate_preview(
            session, context=context, draft_id=prepared, expected_revision=1
        )
        == identity
    )
    drain(session, context, identity)
    summary = previews.get_preview_summary(
        session, context=context, preview_id=identity
    )
    assert summary.status == "FROZEN"
    assert (summary.campaign_count, summary.adgroup_count, summary.ad_count) == (
        6,
        18,
        36,
    )
    assert Decimal(summary.daily_budget_sum) == 600
    units = previews.get_preview_units(
        session, context=context, preview_id=identity
    ).items
    assert len(units) == 6 and all(x.readiness == "PREPARING" for x in units)
    by_drama = {}
    for unit in units:
        frozen = previews.load_frozen_unit(
            session, context=context, unit_id=unit.unit_id
        )
        assert frozen.budget == 100 and frozen.campaign_name.startswith(
            frozen.protected_base
        )
        groups = previews.get_frozen_groups(
            session, context=context, unit_id=unit.unit_id
        ).items
        assert [len(g.material_ids) for g in groups] == [10, 10, 3]
        copies = [tuple((ad.copy_id, ad.text) for ad in group.ads) for group in groups]
        if unit.drama_id in by_drama:
            assert by_drama[unit.drama_id] == copies
        by_drama[unit.drama_id] = copies
    assert summary.content_digest and len(summary.content_digest) == 64


def test_edit_obsoletes_preview_but_preserves_frozen_intent(session, context, prepared):
    identity = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    drain(session, context, identity)
    units = previews.get_preview_units(
        session, context=context, preview_id=identity
    ).items
    before = previews.load_frozen_unit(
        session, context=context, unit_id=units[0].unit_id
    )
    drama = session.exec(
        select(DraftDrama).where(DraftDrama.draft_id == prepared)
    ).first()
    edit_material_groups(
        session,
        context=context,
        draft_id=prepared,
        drama_id=drama.drama_id,
        expected_revision=1,
        groups=[],
    )
    assert (
        previews.get_preview_summary(
            session, context=context, preview_id=identity
        ).status
        == "OBSOLETE"
    )
    assert (
        previews.load_frozen_unit(session, context=context, unit_id=units[0].unit_id)
        == before
    )


def test_per_pair_blocks_and_budget_are_exact(session, context, prepared, monkeypatch):
    from dataclasses import replace

    original = previews.read_scene_context

    def read(*args, **kwargs):
        scene = original(*args, **kwargs)
        return (
            replace(scene, supported=False, reason_codes=("minis_unavailable",))
            if kwargs["advertiser_id"] == "C"
            else scene
        )

    monkeypatch.setattr(previews, "read_scene_context", read)
    identity = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    drain(session, context, identity)
    summary = previews.get_preview_summary(
        session, context=context, preview_id=identity
    )
    assert (
        summary.campaign_count,
        summary.blocked_count,
        summary.daily_budget_sum,
    ) == (4, 2, Decimal("400"))
    assert summary.adgroup_count == 12 and summary.ad_count == 24
    units = previews.get_preview_units(
        session, context=context, preview_id=identity, readiness="BLOCKED"
    ).items
    assert len(units) == 2 and all(u.advertiser_id == "C" for u in units)


def test_preview_tables_frozen_and_material_upload_does_not_expand_intent(
    session, context, prepared
):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    identity = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    drain(session, context, identity)
    before = previews.get_preview_summary(session, context=context, preview_id=identity)
    material(session, context, "Moon new.mp4", bc="bc-draft")
    assert (
        previews.get_preview_summary(session, context=context, preview_id=identity)
        == before
    )
    for table in [
        "build_preview",
        "build_unit",
        "planned_ad",
        "preview_group_material",
        "preview_copy",
        "preview_drama",
    ]:
        with pytest.raises(DBAPIError), session.begin_nested():
            session.execute(
                text(
                    f"DELETE FROM {table} WHERE "
                    + ("id=:id" if table == "build_preview" else "preview_id=:id")
                ),
                {"id": identity},
            )


def test_bounded_restart_and_paged_reads_preserve_rows(
    session, context, other_context, prepared, monkeypatch
):
    from app.core.errors import DomainError
    from app.modules.builds.preview_models import BuildPreview
    from app.modules.materials import service

    def forbidden(*_a, **_kw):
        raise AssertionError("Preview requested a remote material write")

    monkeypatch.setattr(service, "ensure_target_asset", forbidden)
    identity = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    assert not previews.continue_preview(
        session, context=context, preview_id=identity, step_limit=1
    )
    original = session.get(BuildPreview, identity).batch_short_id
    session.expire_all()
    drain(session, context, identity)
    assert session.get(BuildPreview, identity).batch_short_id == original
    result = []
    cursor = None
    while True:
        page = previews.get_preview_units(
            session, context=context, preview_id=identity, cursor=cursor, limit=2
        )
        result.extend(page.items)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    assert len(result) == len({u.unit_id for u in result}) == 6
    with pytest.raises(DomainError):
        previews.get_preview_units(
            session, context=other_context, preview_id=identity, cursor=cursor
        )
    with pytest.raises(DomainError):
        previews.get_preview_units(
            session,
            context=context,
            preview_id=identity,
            cursor=cursor,
            readiness="BLOCKED",
        )


def test_revision_during_build_keeps_partial_preview_obsolete(
    session, context, prepared
):
    from app.modules.builds.preview_models import BuildPreview

    identity = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    assert not previews.continue_preview(
        session, context=context, preview_id=identity, step_limit=1
    )
    drama = session.exec(
        select(DraftDrama).where(DraftDrama.draft_id == prepared)
    ).first()
    edit_material_groups(
        session,
        context=context,
        draft_id=prepared,
        drama_id=drama.drama_id,
        expected_revision=1,
        groups=[],
    )
    assert previews.continue_preview(session, context=context, preview_id=identity)
    row = session.get(BuildPreview, identity, populate_existing=True)
    assert row.status == "OBSOLETE" and row.content_digest is None


def test_identical_protected_names_block_both_pairs(session, context, prepared):
    from app.modules.providers.models import PromotionLink

    dramas = session.exec(
        select(DraftDrama).where(DraftDrama.draft_id == prepared)
    ).all()
    for drama in dramas:
        link = session.get(PromotionLink, drama.link_id)
        link.protected_base = "same-provider-base"
        session.add(link)
    session.flush()
    identity = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    drain(session, context, identity)
    summary = previews.get_preview_summary(
        session, context=context, preview_id=identity
    )
    assert summary.campaign_count == 0 and summary.blocked_count == 6
    assert all(
        "duplicate_campaign_name" in u.reason_codes
        for u in previews.get_preview_units(
            session, context=context, preview_id=identity
        ).items
    )


def test_large_cjk_name_is_reported_without_database_index_error(
    session, context, prepared
):
    from app.modules.providers.models import PromotionLink

    drama = session.exec(
        select(DraftDrama).where(DraftDrama.draft_id == prepared)
    ).first()
    link = session.get(PromotionLink, drama.link_id)
    link.protected_base = "剧" * 1000
    session.add(link)
    session.flush()
    identity = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    drain(session, context, identity)
    summary = previews.get_preview_summary(
        session, context=context, preview_id=identity
    )
    assert summary.blocked_count == 3 and summary.campaign_count == 3


def test_worker_duplicate_and_repair_reuse_published_identity(
    session, context, prepared
):
    from datetime import UTC, datetime, timedelta

    from app.jobs.models import PendingDispatch
    from app.modules.builds.preview_models import BuildPreview
    from app.modules.builds.preview_tasks import process_preview, repair_previews

    identity = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    header = session.get(BuildPreview, identity)
    original = header.dispatch_id
    header.repair_after = datetime.now(UTC) - timedelta(seconds=1)
    dispatch = session.get(PendingDispatch, original)
    dispatch.published_at = datetime.now(UTC) - timedelta(minutes=4)
    session.add_all([header, dispatch])
    session.flush()
    assert repair_previews(database_engine=session.connection(), limit=1) == 1
    session.expire_all()
    assert session.get(BuildPreview, identity).dispatch_id == original
    assert session.get(PendingDispatch, original).published_at is None
    for _ in range(2):
        process_preview(
            database_engine=session.connection(),
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            payload={"preview_id": str(identity), "generation": 0},
        )
    session.expire_all()
    assert session.get(BuildPreview, identity).status == "FROZEN"


def test_http_preview_request_and_readonly_pages(
    session, client, context, other_context, prepared
):
    from sqlalchemy import func

    from app.jobs.models import PendingDispatch
    from app.modules.tenants.models import TenantMembership
    from tests.modules.strategies.test_api import headers

    base = f"/api/tenants/{context.tenant_id}"
    response = client.post(
        f"{base}/build-drafts/{prepared}/previews",
        json={"expected_revision": 1},
        headers=headers(context),
    )
    assert response.status_code == 202
    identity = UUID(response.json()["preview_id"])
    assert (
        client.get(
            f"{base}/build-drafts/{prepared}/previews/1", headers=headers(context)
        ).json()
        == response.json()
    )
    drain(session, context, identity)
    before = session.exec(select(func.count()).select_from(PendingDispatch)).one()
    summary = client.get(f"{base}/build-previews/{identity}", headers=headers(context))
    assert (
        summary.status_code == 200
        and Decimal(summary.json()["daily_budget_sum"]) == 600
    )
    page = client.get(
        f"{base}/build-previews/{identity}/units?limit=2", headers=headers(context)
    ).json()
    unit = page["items"][0]["unit_id"]
    assert (
        client.get(
            f"{base}/build-units/{unit}/groups", headers=headers(context)
        ).status_code
        == 200
    )
    assert (
        client.get(
            f"{base}/build-previews/{identity}/inputs?kind=drama",
            headers=headers(context),
        ).status_code
        == 200
    )
    assert (
        client.get(
            f"/api/tenants/{other_context.tenant_id}/build-units/{unit}",
            headers=headers(other_context),
        ).status_code
        == 404
    )
    member = session.exec(
        select(TenantMembership).where(
            TenantMembership.tenant_id == context.tenant_id,
            TenantMembership.user_id == context.actor_id,
        )
    ).one()
    member.role = "viewer"
    session.add(member)
    session.flush()
    assert (
        client.get(f"{base}/build-units/{unit}", headers=headers(context)).status_code
        == 200
    )
    assert (
        client.post(
            f"{base}/build-drafts/{prepared}/previews",
            json={"expected_revision": 1},
            headers=headers(context),
        ).status_code
        == 403
    )
    assert (
        session.exec(select(func.count()).select_from(PendingDispatch)).one() == before
    )


@pytest.mark.parametrize("mode", ["generate", "continue", "edit"])
def test_preview_concurrent_parent_locking(isolated_strategy_database, mode):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlmodel import Session

    from app.modules.builds.preview_models import BuildPreview
    from tests.modules.builds.test_drafts import create_intent

    engine, context, _ = isolated_strategy_database
    with Session(engine) as session:
        values = create_intent(session, context)
        draft = create_draft(session, context=context, **values)
        task = prepare_draft(
            session, context=context, draft_id=draft, request_id=uuid4()
        )
        ready_links(session, context, task, values)
        finish(session, context, task)
        drama = (
            session.exec(select(DraftDrama).where(DraftDrama.draft_id == draft))
            .first()
            .drama_id
        )
        identity = (
            previews.generate_preview(
                session, context=context, draft_id=draft, expected_revision=1
            )
            if mode != "generate"
            else None
        )
        session.commit()
    barrier = Barrier(2)

    def work(index):
        with Session(engine) as session:
            barrier.wait(timeout=10)
            if mode == "generate":
                result = previews.generate_preview(
                    session, context=context, draft_id=draft, expected_revision=1
                )
            elif mode == "edit" and index == 1:
                result = edit_material_groups(
                    session,
                    context=context,
                    draft_id=draft,
                    drama_id=drama,
                    expected_revision=1,
                    groups=[],
                )
            else:
                result = previews.continue_preview(
                    session, context=context, preview_id=identity
                )
            session.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(work, range(2)))
    with Session(engine) as session:
        rows = session.exec(
            select(BuildPreview).where(BuildPreview.draft_id == draft)
        ).all()
        assert len(rows) == 1
        if mode == "generate":
            assert result[0] == result[1]
        elif mode == "continue":
            assert rows[0].status == "FROZEN"
        else:
            assert rows[0].status == "OBSOLETE"
