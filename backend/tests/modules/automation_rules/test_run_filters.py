"""执行记录按结果、日期、账户与剧名筛选。"""

from datetime import UTC, date, datetime, timedelta

from app.modules.automation_rules.models import AutomationRuleHit, AutomationRuleRun
from app.modules.automation_rules.service import RunFilters, list_runs
from tests.modules.automation_rules.test_runner import make_rule

DAY = datetime(2026, 10, 14, 4, 0, tzinfo=UTC)  # 上海 12:00


def _run(
    session,
    rule,
    started,
    *,
    hits=0,
    status="COMPLETE",
    execution="NOT_REQUIRED",
    summary=None,
    hit_rows=(),
):
    run = AutomationRuleRun(
        tenant_id=rule.tenant_id,
        rule_id=rule.id,
        rule_version=1,
        definition=rule.definition,
        slot_at=started,
        started_at=started,
        status=status,
        hit_count=hits,
        execution_status=execution,
        execution_summary=summary or {},
    )
    session.add(run)
    session.flush()
    for sequence, (advertiser, drama) in enumerate(hit_rows):
        session.add(
            AutomationRuleHit(
                tenant_id=rule.tenant_id,
                run_id=run.id,
                sequence=sequence,
                subject_key=f"adgroup:{advertiser}:{sequence}",
                subject_label="g",
                advertiser_id=advertiser,
                drama_name=drama,
                metrics={},
                targets=[],
                target_count=1,
            )
        )
    session.flush()
    return run


def _ids(page):
    return {item.id for item in page.items}


def test_filters(session, sync_seed, other_context):
    rule = make_rule(session, sync_seed)
    no_hit = _run(session, rule, DAY)
    hit = _run(
        session,
        rule,
        DAY - timedelta(hours=1),
        hits=1,
        hit_rows=[("acc-1", "嘉书-总裁归来")],
    )
    failed = _run(session, rule, DAY - timedelta(days=2), status="FAILED")
    executed = _run(
        session,
        rule,
        DAY - timedelta(hours=2),
        hits=1,
        execution="PARTIAL",
        summary={"submitted": 1, "conflicts": 1},
        hit_rows=[("acc-2", "嘉书-闪婚")],
    )
    query = lambda **kw: list_runs(  # noqa: E731
        session,
        context=sync_seed.context,
        rule_id=None,
        limit=50,
        offset=0,
        filters=RunFilters(**kw),
    )
    assert _ids(query(result="NO_HIT")) == {no_hit.id}
    assert _ids(query(result="HIT")) == {hit.id, executed.id}
    assert _ids(query(result="FAILED")) == {failed.id}
    assert _ids(query(result="EXECUTED")) == {executed.id}
    assert _ids(query(result="CONFLICT")) == {executed.id}
    assert _ids(query(advertiser_id="acc-1")) == {hit.id}
    assert _ids(query(drama="闪婚")) == {executed.id}
    # 日期按租户运行时区（默认 Asia/Shanghai）解释。
    assert _ids(query(since=date(2026, 10, 14), until=date(2026, 10, 14))) == {
        no_hit.id,
        hit.id,
        executed.id,
    }
    assert query(drama="闪婚").total == 1
    other = list_runs(
        session,
        context=other_context,
        rule_id=None,
        limit=50,
        offset=0,
        filters=RunFilters(),
    )
    assert other.items == []
