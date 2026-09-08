"""Independent checks for bounded Cartesian expansion and snapshot read safety."""

from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from sqlmodel import select

from app.jobs.models import PendingDispatch
from app.modules.accounts.models import BCAccountAccess
from app.modules.builds import previews
from app.modules.builds.models import DraftAccount
from app.modules.builds.preview_models import BuildPreview
from app.modules.builds.preview_tasks import repair_previews
from tests.modules.builds.test_drafts import account
from tests.modules.builds.test_previews import drain
from tests.modules.builds.test_previews import prepared as prepared


def test_503_accounts_expand_every_drama_without_bulk_account_materialization(
    session, context, prepared
):
    for i in range(500):
        identity = f"extra-{i:04}"
        account(session, context, identity)
        grant = session.exec(
            select(BCAccountAccess).where(
                BCAccountAccess.tenant_id == context.tenant_id,
                BCAccountAccess.advertiser_id == identity,
            )
        ).one()
        session.add(
            DraftAccount(
                tenant_id=context.tenant_id,
                draft_id=prepared,
                advertiser_id=identity,
                bc_id="bc-draft",
                connection_id=grant.connection_id,
                currency="USD",
                timezone="UTC",
                first_line=i + 4,
            )
        )
    session.flush()
    identity = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    queries = []

    def query(_c, _cur, statement, _params, _ctx, _many):
        if "FROM draft_account" in statement and statement.lstrip().startswith(
            "SELECT"
        ):
            queries.append(statement)

    event.listen(session.connection(), "before_cursor_execute", query)
    calls = 0
    try:
        while True:
            calls += 1
            done = previews.continue_preview(
                session, context=context, preview_id=identity, step_limit=200
            )
            # Round-trip identity map to exercise persistent progress, not Python locals.
            session.expire_all()
            if done:
                break
            assert calls < 100
    finally:
        event.remove(session.connection(), "before_cursor_execute", query)
    assert calls > 1 and queries and all("LIMIT" in query for query in queries)
    summary = previews.get_preview_summary(
        session, context=context, preview_id=identity
    )
    assert summary.status == "FROZEN"
    assert (
        summary.total_unit_count,
        summary.campaign_count,
        summary.adgroup_count,
        summary.ad_count,
    ) == (1006, 1006, 3018, 6036)
    assert summary.daily_budget_sum == Decimal("100600")
    cursor, counts = None, Counter()
    while True:
        page = previews.get_preview_units(
            session, context=context, preview_id=identity, cursor=cursor, limit=100
        )
        counts.update(unit.advertiser_id for unit in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert len(counts) == 503 and set(counts.values()) == {2}


def test_repair_preserves_unpublished_backoff_identity_and_generation(
    session, context, prepared
):
    identity = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    header = session.get(BuildPreview, identity)
    message = session.get(PendingDispatch, header.dispatch_id)
    message.available_at = datetime.now(UTC) + timedelta(hours=2)
    header.repair_after = datetime.now(UTC) - timedelta(minutes=1)
    old = (message.id, message.available_at, dict(message.payload), header.generation)
    session.flush()
    assert repair_previews(database_engine=session.connection(), limit=1) == 1
    session.expire_all()
    header = session.get(BuildPreview, identity)
    message = session.get(PendingDispatch, header.dispatch_id)
    assert (message.id, message.available_at, message.payload, header.generation) == old


def test_frozen_rows_reject_header_and_child_updates(session, context, prepared):
    identity = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    drain(session, context, identity)
    for statement in (
        "UPDATE build_preview SET budget=1 WHERE id=:id",
        "UPDATE build_unit SET advertiser_id='B' WHERE preview_id=:id",
        "UPDATE preview_drama SET title='changed' WHERE preview_id=:id",
        "UPDATE planned_ad SET text='changed' WHERE preview_id=:id",
    ):
        with pytest.raises(DBAPIError), session.begin_nested():
            session.execute(text(statement), {"id": identity})
    assert (
        previews.get_preview_summary(
            session, context=context, preview_id=identity
        ).daily_budget_sum
        == 600
    )


@pytest.mark.parametrize(
    "broken,expected",
    [
        ("cta", "cta_options_unavailable"),
        ("copy", "field_limits_unverified"),
        ("creative", "creative_count_exceeded"),
        ("budget", "budget_out_of_range"),
        ("measurement", "field_limits_unverified"),
    ],
)
def test_incomplete_scene_constraints_block_real_unit_counts(
    session, context, prepared, monkeypatch, broken, expected
):
    from dataclasses import replace

    source = previews.read_scene_context

    def changed(*args, **kwargs):
        original = source(*args, **kwargs)
        if broken == "cta":
            return replace(original, cta_fields={})
        if broken == "copy":
            return replace(original, copy_length_limit=0)
        limits = dict(original.to_snapshot()["field_constraints"])
        if broken == "creative":
            limits["max_ads_per_adgroup"] = 1
        if broken == "budget":
            limits["campaign_daily_budget"]["minimum_inclusive"] = "101"
        if broken == "measurement":
            limits["name_measurement"]["campaign"] = "unsupported-rule"
        return replace(original, field_constraints=limits)

    monkeypatch.setattr(previews, "read_scene_context", changed)
    identity = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    drain(session, context, identity)
    summary = previews.get_preview_summary(
        session, context=context, preview_id=identity
    )
    assert (
        summary.status == "FROZEN"
        and summary.total_unit_count == summary.blocked_count == 6
    )
    assert (
        summary.campaign_count
        == summary.adgroup_count
        == summary.ad_count
        == summary.daily_budget_sum
        == 0
    )
    assert all(
        expected in unit.reason_codes
        for unit in previews.get_preview_units(
            session, context=context, preview_id=identity
        ).items
    )
