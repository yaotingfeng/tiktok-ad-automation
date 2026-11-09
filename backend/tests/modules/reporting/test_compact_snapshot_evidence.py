"""所有维度快照保留最小身份，重引用只在实际操作/页内按原版本展开。"""

from sqlalchemy import text

from tests.modules.reporting.test_queries import filters_for, freeze, query


def test_scalar_snapshot_does_not_copy_refs_or_expand_name_history(report_case):
    case = report_case
    original = case.seed_campaign("P-Old", "report-account", 4, 8)
    page = query(case, filters=filters_for())
    stored = (
        case.session.connection()
        .execute(
            text(
                "SELECT refs,material_uses FROM query_snapshot_row WHERE snapshot_id=:s"
            ),
            {"s": page.snapshot.snapshot_id},
        )
        .one()
    )
    assert stored.refs == []
    assert stored.material_uses == []
    assert page.items[0].refs == (original,)
    case.seed_campaign("P-New", "report-account", 1, 2)
    assert freeze(case, page.snapshot.snapshot_id).refs == (original,)


def test_drama_freezes_compact_pairs_and_original_names(report_case):
    from app.modules.ads.directory import append_campaign_name_projection

    case = report_case
    original = case.seed_campaign("嘉书-旧剧-备注", "report-account", 4, 8)
    append_campaign_name_projection(
        case.session,
        campaign_ref=original,
        raw_name="嘉书-旧剧-备注",
        parser_revision=1,
    )
    page = query(case, filters=filters_for("drama"))
    stored = (
        case.session.connection()
        .execute(
            text("SELECT refs FROM query_snapshot_row WHERE snapshot_id=:s"),
            {"s": page.snapshot.snapshot_id},
        )
        .scalar_one()
    )
    assert stored == []
    append_campaign_name_projection(
        case.session,
        campaign_ref=original,
        raw_name="嘉书-新剧-备注",
        parser_revision=1,
    )
    assert freeze(case, page.snapshot.snapshot_id).refs == (original,)
