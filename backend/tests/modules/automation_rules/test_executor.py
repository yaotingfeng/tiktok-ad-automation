"""执行器：真实冻结选择、预览与管理任务提交；不执行 provider 写入（只入队）。"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, select

from app.core.db import engine
from app.modules.accounts.connection_models import (
    BCConnectionBinding,
    BCDefaultRoute,
    ConnectionAuthorization,
)
from app.modules.accounts.models import (
    AdvertiserAccount,
    BCAccountAccess,
    TenantBC,
    TikTokConnection,
)
from app.modules.ad_management.models import ManagementTask
from app.modules.ads.models import AdObject
from app.modules.automation_rules.executor import execute_hits
from app.modules.automation_rules.models import (
    AutomationActionLedger,
    AutomationRule,
    AutomationRuleHit,
    AutomationRuleRun,
    AutomationTenantSetting,
)
from app.modules.tenants.models import TenantMembership
from tests.modules.automation_rules.test_definition import base
from tests.modules.conftest import create_context

NOW = datetime(2026, 10, 14, 4, 0, tzinfo=UTC)


def _bc(session, context, bc_id, account_id, *, default_route=True):
    connection = TikTokConnection(tenant_id=context.tenant_id, status="ACTIVE")
    session.add_all(
        [
            connection,
            TenantBC(tenant_id=context.tenant_id, bc_id=bc_id),
            AdvertiserAccount(
                tenant_id=context.tenant_id,
                advertiser_id=account_id,
                currency="USD",
                timezone="UTC",
                remote_status="STATUS_ENABLE",
            ),
        ]
    )
    session.flush()
    session.add_all(
        [
            BCAccountAccess(
                tenant_id=context.tenant_id,
                bc_id=bc_id,
                advertiser_id=account_id,
                connection_id=connection.id,
                in_bc=True,
                authorized=True,
                active=True,
                can_build=True,
                can_upload=True,
                permission_state="VERIFIED",
                checked_at=datetime.now(UTC),
            ),
            BCConnectionBinding(
                tenant_id=context.tenant_id,
                bc_id=bc_id,
                connection_id=connection.id,
                kind=connection.kind,
            ),
            ConnectionAuthorization(
                tenant_id=context.tenant_id,
                connection_id=connection.id,
                authorization_revision=connection.authorization_revision,
                scopes=["2", "6"],
                permission_summary={"read_authorized": True, "build_authorized": True},
                source="SYNTHETIC_COMPLETE_EVIDENCE",
                verified_at=datetime.now(UTC),
            ),
        ]
    )
    if default_route:
        session.add(
            BCDefaultRoute(
                tenant_id=context.tenant_id, bc_id=bc_id, connection_id=connection.id
            )
        )
    session.flush()
    return connection


def _objects(session, context, connection, account_id, *, budget="50", status="ENABLE"):
    for kind, rid, parent, config in (
        ("campaign", "c1", None, {"budget_mode": "BUDGET_MODE_INFINITE"}),
        ("adgroup", "g1", "c1", {"budget": budget}),
    ):
        session.add(
            AdObject(
                tenant_id=context.tenant_id,
                advertiser_id=account_id,
                kind=kind,
                remote_id=rid,
                parent_kind="campaign" if parent else None,
                parent_remote_id=parent,
                ad_type="REGULAR",
                name=f"{kind}-{rid}",
                configuration=config,
                operation_status=status,
                review_status="APPROVED",
                delivery_status="DELIVERING",
                observed_at=datetime.now(UTC),
                published_version=1,
                source_connection_id=connection.id,
                source_channel="OFFICIAL_API",
            )
        )
    session.flush()


def _target(account_id, bc_id):
    return {
        "key": f"adgroup:{account_id}:g1",
        "kind": "adgroup",
        "advertiser_id": account_id,
        "bc_id": bc_id,
        "remote_id": "g1",
        "name": "adgroup-g1",
        "material_uses": [],
    }


def _rule_run(session, context, *, action, targets, name="r", frequency=None):
    if action["type"] in ("BUDGET", "ROAS"):
        action = {**action, "frequency": frequency or {"type": "EVERY_24H"}}
    # 素材相关规则最短 1 小时运行一次。
    schedule = {
        "frequency": "1H" if action["level"] == "material" else "30M",
        "window": "CONTINUOUS",
    }
    rule = AutomationRule(
        tenant_id=context.tenant_id,
        name=name,
        created_by=context.actor_id,
        updated_by=context.actor_id,
        definition=base(action=action, mode="AUTO", schedule=schedule),
    )
    session.add(rule)
    session.flush()
    run = AutomationRuleRun(
        tenant_id=context.tenant_id,
        rule_id=rule.id,
        rule_version=1,
        definition=rule.definition,
        slot_at=NOW,
        status="COMPLETE",
        hit_count=len(targets),
        mode="AUTO",
    )
    session.add(run)
    session.flush()
    for sequence, target in enumerate(targets):
        session.add(
            AutomationRuleHit(
                tenant_id=context.tenant_id,
                run_id=run.id,
                sequence=sequence,
                subject_key=target["key"],
                subject_label=target["name"],
                metrics={},
                targets=[target],
                target_count=1,
            )
        )
    session.flush()
    return rule, run


def _hits(session, run):
    return {
        hit.subject_key: hit.outcome
        for hit in session.exec(
            select(AutomationRuleHit).where(AutomationRuleHit.run_id == run.id)
        ).all()
    }


@pytest.fixture
def env(session):
    context = create_context(session, role="tenant_admin")
    connection = _bc(session, context, "bc-x", "acc-x")
    _objects(session, context, connection, "acc-x")
    return context


PAUSE = {"type": "PAUSE", "level": "adgroup"}
BUDGET_UP = {
    "type": "BUDGET",
    "level": "adgroup",
    "adjustment": {
        "mode": "INCREASE",
        "value_type": "PERCENT",
        "value": "20",
        "limit": "1000",
    },
}


def test_auto_execution_submits_management_task(session, env):
    rule, run = _rule_run(
        session, env, action=PAUSE, targets=[_target("acc-x", "bc-x")]
    )
    summary = execute_hits(
        session,
        run=run,
        rule=rule,
        selected_keys=None,
        now=NOW,
        actor_id=rule.updated_by,
        require_manage=True,
    )
    assert summary["submitted"] == 1 and run.execution_status == "SUBMITTED"
    tasks = session.exec(
        select(ManagementTask).where(ManagementTask.tenant_id == env.tenant_id)
    ).all()
    assert len(tasks) == 1 and summary["tasks"] == [
        {"bc_id": "bc-x", "task_id": str(tasks[0].id)}
    ]
    ledger = session.exec(select(AutomationActionLedger)).all()
    assert [(row.target_key, row.action) for row in ledger] == [
        ("adgroup:acc-x:g1", "PAUSE")
    ]
    assert _hits(session, run) == {"adgroup:acc-x:g1": "SUBMITTED"}


def test_frequency_once_skips_second_run(session, env):
    rule, run = _rule_run(
        session,
        env,
        action=BUDGET_UP,
        targets=[_target("acc-x", "bc-x")],
        frequency={"type": "ONCE"},
    )
    execute_hits(
        session,
        run=run,
        rule=rule,
        selected_keys=None,
        now=NOW,
        actor_id=rule.updated_by,
        require_manage=True,
    )
    second = AutomationRuleRun(
        tenant_id=env.tenant_id,
        rule_id=rule.id,
        rule_version=1,
        definition=rule.definition,
        slot_at=NOW + timedelta(hours=1),
        status="COMPLETE",
        mode="AUTO",
    )
    session.add(second)
    session.flush()
    session.add(
        AutomationRuleHit(
            tenant_id=env.tenant_id,
            run_id=second.id,
            sequence=0,
            subject_key="adgroup:acc-x:g1",
            subject_label="g",
            metrics={},
            targets=[_target("acc-x", "bc-x")],
            target_count=1,
        )
    )
    session.flush()
    summary = execute_hits(
        session,
        run=second,
        rule=rule,
        selected_keys=None,
        now=NOW + timedelta(hours=1),
        actor_id=rule.updated_by,
        require_manage=True,
    )
    assert summary["skipped_frequency"] == 1 and summary["submitted"] == 0
    assert _hits(session, second) == {"adgroup:acc-x:g1": "SKIPPED_FREQUENCY"}


def test_reverse_action_within_cooldown_is_conflict(session, env):
    other, other_run = _rule_run(
        session,
        env,
        action={"type": "ENABLE", "level": "adgroup"},
        targets=[],
        name="other",
    )
    session.add(
        AutomationActionLedger(
            tenant_id=env.tenant_id,
            rule_id=other.id,
            run_id=other_run.id,
            target_key="adgroup:acc-x:g1",
            action="ENABLE",
            created_at=NOW - timedelta(hours=1),
        )
    )
    rule, run = _rule_run(
        session, env, action=PAUSE, targets=[_target("acc-x", "bc-x")]
    )
    summary = execute_hits(
        session,
        run=run,
        rule=rule,
        selected_keys=None,
        now=NOW,
        actor_id=rule.updated_by,
        require_manage=True,
    )
    assert summary["conflicts"] == 1 and summary["submitted"] == 0
    assert summary["conflict_details"] == [
        {"target_key": "adgroup:acc-x:g1", "rule_name": "other", "action": "ENABLE"}
    ]
    assert session.exec(select(ManagementTask)).all() == []


def test_daily_increase_limit_excludes_target(session, env):
    session.add(
        AutomationTenantSetting(tenant_id=env.tenant_id, daily_increase_limit_pct=100)
    )
    up30 = {**BUDGET_UP, "adjustment": {**BUDGET_UP["adjustment"], "value": "30"}}
    rule, run = _rule_run(
        session,
        env,
        action=up30,
        targets=[_target("acc-x", "bc-x")],
        frequency={"type": "CUSTOM", "window": 1, "unit": "MINUTE", "count": 9},
    )
    # 当日早些时候已从 30 提到 50；本次 50→65 超过基准 30 的 2 倍（日涨幅 100%）即 60。
    session.add(
        AutomationActionLedger(
            tenant_id=env.tenant_id,
            rule_id=rule.id,
            run_id=run.id,
            target_key="adgroup:acc-x:g1",
            action="BUDGET_UP",
            original_value="30",
            final_value="50",
            created_at=NOW - timedelta(hours=2),
        )
    )
    session.flush()
    summary = execute_hits(
        session,
        run=run,
        rule=rule,
        selected_keys=None,
        now=NOW,
        actor_id=rule.updated_by,
        require_manage=True,
    )
    assert summary["skipped_daily_limit"] == 1 and summary["submitted"] == 0
    assert _hits(session, run) == {"adgroup:acc-x:g1": "SKIPPED_DAILY_LIMIT"}


def test_no_change_targets_skip_ledger(session, env):
    capped = {**BUDGET_UP, "adjustment": {**BUDGET_UP["adjustment"], "limit": "50"}}
    rule, run = _rule_run(
        session, env, action=capped, targets=[_target("acc-x", "bc-x")]
    )
    summary = execute_hits(
        session,
        run=run,
        rule=rule,
        selected_keys=None,
        now=NOW,
        actor_id=rule.updated_by,
        require_manage=True,
    )
    assert summary["no_change"] == 1 and summary["submitted"] == 0
    assert session.exec(select(AutomationActionLedger)).all() == []
    assert _hits(session, run) == {"adgroup:acc-x:g1": "NO_CHANGE"}


def test_owner_without_manage_is_not_executed(session, env):
    rule, run = _rule_run(
        session, env, action=PAUSE, targets=[_target("acc-x", "bc-x")]
    )
    session.get(TenantMembership, (env.tenant_id, env.actor_id)).role = "operator"
    session.flush()
    execute_hits(
        session,
        run=run,
        rule=rule,
        selected_keys=None,
        now=NOW,
        actor_id=rule.updated_by,
        require_manage=True,
    )
    assert run.execution_status == "FAILED"
    assert run.execution_summary["error"] == "rule_owner_forbidden"
    assert session.exec(select(ManagementTask)).all() == []


def test_one_bc_failure_does_not_block_others(session, env):
    _bc(session, env, "bc-y", "acc-y", default_route=False)
    rule, run = _rule_run(
        session,
        env,
        action=PAUSE,
        targets=[_target("acc-x", "bc-x"), _target("acc-y", "bc-y")],
    )
    summary = execute_hits(
        session,
        run=run,
        rule=rule,
        selected_keys=None,
        now=NOW,
        actor_id=rule.updated_by,
        require_manage=True,
    )
    assert run.execution_status == "PARTIAL" and summary["submitted"] == 1
    assert [item["bc_id"] for item in summary["failed_bcs"]] == ["bc-y"]
    assert _hits(session, run) == {
        "adgroup:acc-x:g1": "SUBMITTED",
        "adgroup:acc-y:g1": "FAILED",
    }


def test_selected_keys_limit_execution(session, env):
    _bc(session, env, "bc-y", "acc-y")
    rule, run = _rule_run(
        session,
        env,
        action=PAUSE,
        targets=[_target("acc-x", "bc-x"), _target("acc-y", "bc-y")],
    )
    execute_hits(
        session,
        run=run,
        rule=rule,
        selected_keys={"adgroup:acc-x:g1"},
        now=NOW,
        actor_id=rule.updated_by,
        require_manage=False,
    )
    assert _hits(session, run) == {
        "adgroup:acc-x:g1": "SUBMITTED",
        "adgroup:acc-y:g1": "NOT_SELECTED",
    }


def test_concurrent_reverse_actions_conflict():
    """两条相反规则在两个独立事务中执行：租户级锁保证后者看到前者的台账。"""
    with Session(engine, expire_on_commit=False) as setup:
        context = create_context(setup, role="tenant_admin")
        connection = _bc(setup, context, "bc-race", "acc-race")
        _objects(setup, context, connection, "acc-race")
        a, run_a = _rule_run(
            setup,
            context,
            action=PAUSE,
            targets=[_target("acc-race", "bc-race")],
            name="a",
        )
        b, run_b = _rule_run(
            setup,
            context,
            action={"type": "ENABLE", "level": "adgroup"},
            targets=[_target("acc-race", "bc-race")],
            name="b",
        )
        setup.commit()
    try:
        results = []
        for rule_id, run_id in ((a.id, run_a.id), (b.id, run_b.id)):
            with Session(engine, expire_on_commit=False) as worker, worker.begin():
                summary = execute_hits(
                    worker,
                    run=worker.get(AutomationRuleRun, run_id),
                    rule=worker.get(AutomationRule, rule_id),
                    selected_keys=None,
                    now=datetime.now(UTC),
                    actor_id=context.actor_id,
                    require_manage=True,
                )
                results.append(summary)
        assert results[0]["submitted"] == 1
        # 第二条：目标已是 DISABLE 时，ENABLE 是相反动作且在冷却期内。
        assert results[1]["conflicts"] == 1 and results[1]["submitted"] == 0
    finally:
        from tests.modules.automation_rules.cleanup import purge_tenant

        purge_tenant(context.tenant_id, context.actor_id)


def test_material_target_uses_material_mode_and_ledger_key(session, env):
    # 素材目标在冻结选择中展开为素材使用；无广告内素材 ID 的普通素材在预览中不可执行。
    target = {
        "key": "material:acc-x:v1",
        "kind": "material",
        "advertiser_id": "acc-x",
        "bc_id": "bc-x",
        "remote_id": "v1",
        "name": "v1",
        "material_uses": [
            {
                "advertiser_id": "acc-x",
                "ad_remote_id": "x1",
                "platform_material_id": "v1",
                "ad_material_id": None,
                "material_type": "VIDEO",
            }
        ],
    }
    rule, run = _rule_run(
        session, env, action={"type": "PAUSE", "level": "material"}, targets=[target]
    )
    summary = execute_hits(
        session,
        run=run,
        rule=rule,
        selected_keys=None,
        now=NOW,
        actor_id=rule.updated_by,
        require_manage=True,
    )
    # 普通广告的素材没有广告内素材 ID，不能单独启停：记为不支持而不是失败。
    assert summary["submitted"] == 0 and summary["failed"] == 0
    assert summary["unsupported"] == 1
    assert summary["unsupported_details"][0]["target_key"] == "material:acc-x:v1"
    assert _hits(session, run)["material:acc-x:v1"] == "UNSUPPORTED"
    assert run.execution_status == "SKIPPED"


def _smart_material(session, context, connection):
    from app.modules.ads.models import AdMaterialReference

    session.add(
        AdObject(
            tenant_id=context.tenant_id,
            advertiser_id="acc-x",
            kind="ad",
            remote_id="x1",
            parent_kind="adgroup",
            parent_remote_id="g1",
            ad_type="UPGRADED_SMART_PLUS",
            name="ad-x1",
            configuration={},
            operation_status="ENABLE",
            review_status="APPROVED",
            delivery_status="DELIVERING",
            observed_at=NOW - timedelta(hours=1),
            published_version=1,
            source_connection_id=connection.id,
            source_channel="OFFICIAL_API",
        )
    )
    session.add(
        AdMaterialReference(
            tenant_id=context.tenant_id,
            advertiser_id="acc-x",
            ad_remote_id="x1",
            platform_material_id="v1",
            ad_material_id="am1",
            material_type="VIDEO",
            name="素材一",
            main_material_id="m1",
            main_material_type="VIDEO",
            operation_status="ENABLE",
            complete=True,
            published_version=1,
        )
    )
    session.flush()
    session.flush()
    # 规则目标按主素材 ID 建键，与平台素材 ID 不同。
    return {
        "key": "material:acc-x:m1",
        "kind": "material",
        "advertiser_id": "acc-x",
        "bc_id": "bc-x",
        "remote_id": "m1",
        "name": "素材一",
        "material_uses": [
            {
                "advertiser_id": "acc-x",
                "ad_remote_id": "x1",
                "platform_material_id": "v1",
                "ad_material_id": "am1",
                "material_type": "VIDEO",
            }
        ],
    }


def test_smart_plus_material_pause_keys_ledger_by_rule_target(session, env):
    connection = session.exec(
        select(TikTokConnection).where(TikTokConnection.tenant_id == env.tenant_id)
    ).first()
    target = _smart_material(session, env, connection)
    rule, run = _rule_run(
        session, env, action={"type": "PAUSE", "level": "material"}, targets=[target]
    )
    summary = execute_hits(
        session,
        run=run,
        rule=rule,
        selected_keys=None,
        now=NOW,
        actor_id=rule.updated_by,
        require_manage=True,
    )
    assert summary["submitted"] == 1 and summary["failed"] == 0
    assert _hits(session, run) == {"material:acc-x:m1": "SUBMITTED"}
    ledger = session.exec(select(AutomationActionLedger)).all()
    assert [(row.target_key, row.action) for row in ledger] == [
        ("material:acc-x:m1", "PAUSE")
    ]


def test_budget_set_increase_is_capped_by_daily_limit(session, env):
    session.add(
        AutomationTenantSetting(tenant_id=env.tenant_id, daily_increase_limit_pct=100)
    )
    set_high = {
        "type": "BUDGET",
        "level": "adgroup",
        "adjustment": {"mode": "SET", "value_type": "AMOUNT", "value": "10000"},
    }
    rule, run = _rule_run(
        session, env, action=set_high, targets=[_target("acc-x", "bc-x")]
    )
    summary = execute_hits(
        session,
        run=run,
        rule=rule,
        selected_keys=None,
        now=NOW,
        actor_id=rule.updated_by,
        require_manage=True,
    )
    # 50 → 10000 远超当日 100% 涨幅上限（基准 50，最多到 100）。
    assert summary["skipped_daily_limit"] == 1 and summary["submitted"] == 0


def test_daily_limit_baseline_counts_earlier_set_raise(session, env):
    session.add(
        AutomationTenantSetting(tenant_id=env.tenant_id, daily_increase_limit_pct=100)
    )
    up30 = {**BUDGET_UP, "adjustment": {**BUDGET_UP["adjustment"], "value": "30"}}
    rule, run = _rule_run(session, env, action=up30, targets=[_target("acc-x", "bc-x")])
    # 同一规则当日（改为提高前）已把预算从 25 调整至 50；本次 50→65 超过基准 25 的 2 倍。
    session.add(
        AutomationActionLedger(
            tenant_id=env.tenant_id,
            rule_id=rule.id,
            run_id=run.id,
            target_key="adgroup:acc-x:g1",
            action="BUDGET_SET",
            original_value="25",
            final_value="50",
            created_at=NOW - timedelta(hours=2),
        )
    )
    session.flush()
    summary = execute_hits(
        session,
        run=run,
        rule=rule,
        selected_keys=None,
        now=NOW,
        actor_id=rule.updated_by,
        require_manage=True,
    )
    assert summary["skipped_daily_limit"] == 1 and summary["submitted"] == 0


def test_all_skipped_run_is_marked_skipped(session, env):
    rule, run = _rule_run(
        session,
        env,
        action=BUDGET_UP,
        targets=[_target("acc-x", "bc-x")],
        frequency={"type": "ONCE"},
    )
    session.add(
        AutomationActionLedger(
            tenant_id=env.tenant_id,
            rule_id=rule.id,
            run_id=run.id,
            target_key="adgroup:acc-x:g1",
            action="BUDGET_UP",
            created_at=NOW - timedelta(days=2),
        )
    )
    session.flush()
    execute_hits(
        session,
        run=run,
        rule=rule,
        selected_keys=None,
        now=NOW,
        actor_id=rule.updated_by,
        require_manage=True,
    )
    assert run.execution_status == "SKIPPED"


def test_unexpected_error_fails_bc_and_restores_counts(session, env, monkeypatch):
    from app.modules.automation_rules import executor

    session.add(
        AutomationTenantSetting(tenant_id=env.tenant_id, daily_increase_limit_pct=10)
    )
    rule, run = _rule_run(
        session, env, action=BUDGET_UP, targets=[_target("acc-x", "bc-x")]
    )
    real = executor.prepare_preview
    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("boom")
        return real(*args, **kwargs)

    monkeypatch.setattr(executor, "prepare_preview", flaky)
    summary = execute_hits(
        session,
        run=run,
        rule=rule,
        selected_keys=None,
        now=NOW,
        actor_id=rule.updated_by,
        require_manage=True,
    )
    # 第一次预览计入的涨幅跳过随保存点回滚，整个 BC 记为失败，不重复计数。
    assert summary["skipped_daily_limit"] == 0 and summary["failed"] == 1
    assert summary["failed_bcs"] == [{"bc_id": "bc-x", "reason": "unexpected_error"}]
    assert run.execution_status == "FAILED"


def test_confirmed_batch_runs_as_confirmer(session, env):
    from app.modules.automation_rules.models import AutomationConfirmation

    rule, run = _rule_run(
        session, env, action=PAUSE, targets=[_target("acc-x", "bc-x")]
    )
    from app.models import User

    confirmer = User(username="confirmer-user", hashed_password="unused")
    session.add(confirmer)
    session.flush()
    session.add(
        TenantMembership(tenant_id=env.tenant_id, user_id=confirmer.id, role="operator")
    )
    # 规则最后修改人已降为只读，人工确认仍以确认人身份执行。
    session.get(TenantMembership, (env.tenant_id, env.actor_id)).role = "viewer"
    session.add(
        AutomationConfirmation(
            tenant_id=env.tenant_id,
            rule_id=rule.id,
            run_id=run.id,
            status="CONFIRMED",
            decided_by=confirmer.id,
            created_at=NOW,
            decided_at=NOW,
        )
    )
    session.flush()
    summary = execute_hits(
        session,
        run=run,
        rule=rule,
        selected_keys=None,
        now=NOW,
        actor_id=confirmer.id,
        require_manage=False,
    )
    assert summary["submitted"] == 1
    task = session.exec(select(ManagementTask)).one()
    assert task.actor_id == confirmer.id


def test_redelivered_execute_with_stale_run_is_ignored(monkeypatch):
    """同一执行被投递两次，第二个工作进程持有过期的运行状态：拿锁后重读，不重复执行。"""
    from app.core.config import settings
    from app.modules.automation_rules.tasks import run_execution

    monkeypatch.setattr(settings, "AUTOMATED_RULES_ENABLED", True)
    with Session(engine, expire_on_commit=False) as setup:
        context = create_context(setup, role="tenant_admin")
        connection = _bc(setup, context, "bc-redo", "acc-redo")
        _objects(setup, context, connection, "acc-redo")
        rule, run = _rule_run(
            setup, context, action=PAUSE, targets=[_target("acc-redo", "bc-redo")]
        )
        run.execution_status = "QUEUED"
        setup.commit()
    data = {"run_id": str(run.id), "mode": "AUTO"}
    try:
        with Session(engine) as stale:
            assert stale.get(AutomationRuleRun, run.id).execution_status == "QUEUED"
            with Session(engine) as first, first.begin():
                assert (
                    run_execution(first, tenant_id=str(context.tenant_id), data=data)
                    == "SUBMITTED"
                )
            result = run_execution(stale, tenant_id=str(context.tenant_id), data=data)
            stale.commit()
        assert result == "SUBMITTED"
        with Session(engine) as check:
            rows = check.exec(
                select(AutomationActionLedger).where(
                    AutomationActionLedger.run_id == run.id
                )
            ).all()
            assert len(rows) == 1
    finally:
        from tests.modules.automation_rules.cleanup import purge_tenant

        purge_tenant(context.tenant_id, context.actor_id)


def test_execute_with_flag_off_fails_visibly(session, env, monkeypatch):
    from app.core.config import settings
    from app.modules.automation_rules.tasks import run_execution

    monkeypatch.setattr(settings, "AUTOMATED_RULES_ENABLED", False)
    rule, run = _rule_run(
        session, env, action=PAUSE, targets=[_target("acc-x", "bc-x")]
    )
    run.execution_status = "QUEUED"
    session.flush()
    result = run_execution(
        session,
        tenant_id=str(env.tenant_id),
        data={"run_id": str(run.id), "mode": "AUTO"},
    )
    assert result == "FAILED" and run.execution_status == "FAILED"
    assert run.execution_summary["error"] == "automation_disabled"
    assert run.notify_status == "PENDING"


def test_confirm_execution_uses_decider(session, env, monkeypatch):
    from app.core.config import settings
    from app.models import User
    from app.modules.automation_rules.models import AutomationConfirmation
    from app.modules.automation_rules.tasks import run_execution

    monkeypatch.setattr(settings, "AUTOMATED_RULES_ENABLED", True)
    rule, run = _rule_run(
        session, env, action=PAUSE, targets=[_target("acc-x", "bc-x")]
    )
    decider = User(username="decider-user", hashed_password="unused")
    session.add(decider)
    session.flush()
    session.add(
        TenantMembership(tenant_id=env.tenant_id, user_id=decider.id, role="operator")
    )
    session.add(
        AutomationConfirmation(
            tenant_id=env.tenant_id,
            rule_id=rule.id,
            run_id=run.id,
            status="CONFIRMED",
            decided_by=decider.id,
            created_at=NOW,
            decided_at=NOW,
        )
    )
    run.execution_status = "CONFIRMED"
    session.flush()
    result = run_execution(
        session,
        tenant_id=str(env.tenant_id),
        data={
            "run_id": str(run.id),
            "mode": "CONFIRM",
            "selected_keys": ["adgroup:acc-x:g1"],
        },
    )
    assert result == "SUBMITTED"
    assert session.exec(select(ManagementTask)).one().actor_id == decider.id


def test_ledger_outlives_deleted_rule_and_old_runs(session, env):

    other, other_run = _rule_run(
        session,
        env,
        action={"type": "ENABLE", "level": "adgroup"},
        targets=[],
        name="other",
    )
    session.add(
        AutomationActionLedger(
            tenant_id=env.tenant_id,
            rule_id=other.id,
            run_id=other_run.id,
            target_key="adgroup:acc-x:g1",
            action="ENABLE",
            created_at=NOW - timedelta(hours=1),
        )
    )
    session.flush()
    # 删除规则后它最近的动作仍在台账中，冷却期冲突照常识别。
    session.delete(other)
    session.flush()
    rule, run = _rule_run(
        session, env, action=PAUSE, targets=[_target("acc-x", "bc-x")]
    )
    summary = execute_hits(
        session,
        run=run,
        rule=rule,
        selected_keys=None,
        now=NOW,
        actor_id=rule.updated_by,
        require_manage=True,
    )
    assert summary["conflicts"] == 1
    assert summary["conflict_details"][0]["rule_name"] == "已删除的规则"


def test_once_frequency_survives_run_cleanup(session, env):
    from app.modules.automation_rules.tasks import cleanup_runs

    rule, old_run = _rule_run(
        session,
        env,
        action=BUDGET_UP,
        targets=[],
        frequency={"type": "ONCE"},
    )
    old_run.started_at = NOW - timedelta(days=40)
    session.add(
        AutomationActionLedger(
            tenant_id=env.tenant_id,
            rule_id=rule.id,
            run_id=old_run.id,
            target_key="adgroup:acc-x:g1",
            action="BUDGET_UP",
            created_at=NOW - timedelta(days=40),
        )
    )
    session.flush()
    cleanup_runs(session, now=NOW)
    run = AutomationRuleRun(
        tenant_id=env.tenant_id,
        rule_id=rule.id,
        rule_version=1,
        definition=rule.definition,
        slot_at=NOW,
        status="COMPLETE",
        mode="AUTO",
    )
    session.add(run)
    session.flush()
    session.add(
        AutomationRuleHit(
            tenant_id=env.tenant_id,
            run_id=run.id,
            sequence=0,
            subject_key="adgroup:acc-x:g1",
            subject_label="g",
            metrics={},
            targets=[_target("acc-x", "bc-x")],
            target_count=1,
        )
    )
    session.flush()
    summary = execute_hits(
        session,
        run=run,
        rule=rule,
        selected_keys=None,
        now=NOW,
        actor_id=rule.updated_by,
        require_manage=True,
    )
    # 「仅一次」不因运行记录清理或超过 31 天而重新允许。
    assert summary["skipped_frequency"] == 1 and summary["submitted"] == 0


def test_budget_owner_redirect_ledgers_rule_target(session):
    """广告组规则调预算、预算在系列上：结果与台账记在规则目标（广告组）上，频控生效。"""
    context = create_context(session, role="tenant_admin")
    connection = _bc(session, context, "bc-cbo", "acc-cbo")
    _objects(session, context, connection, "acc-cbo")
    campaign = session.exec(
        select(AdObject).where(
            AdObject.tenant_id == context.tenant_id, AdObject.kind == "campaign"
        )
    ).one()
    campaign.configuration = {"budget_optimize_on": True, "budget": "100"}
    session.flush()
    rule, run = _rule_run(
        session,
        context,
        action=BUDGET_UP,
        targets=[_target("acc-cbo", "bc-cbo")],
        frequency={"type": "ONCE"},
    )
    summary = execute_hits(
        session,
        run=run,
        rule=rule,
        selected_keys=None,
        now=NOW,
        actor_id=rule.updated_by,
        require_manage=True,
    )
    assert summary["submitted"] == 1 and summary["failed"] == 0
    assert _hits(session, run) == {"adgroup:acc-cbo:g1": "SUBMITTED"}
    ledger = session.exec(
        select(AutomationActionLedger).where(AutomationActionLedger.run_id == run.id)
    ).all()
    assert [row.target_key for row in ledger] == ["adgroup:acc-cbo:g1"]


def test_material_with_mixed_uses_submits_and_ledgers(session, env):
    connection = session.exec(
        select(TikTokConnection).where(TikTokConnection.tenant_id == env.tenant_id)
    ).first()
    target = _smart_material(session, env, connection)
    from app.modules.ads.models import AdMaterialReference

    # 同一主素材还用于一条普通广告：该使用不支持单独启停，但 Smart+ 使用照常执行。
    session.add(
        AdObject(
            tenant_id=env.tenant_id,
            advertiser_id="acc-x",
            kind="ad",
            remote_id="x0",
            parent_kind="adgroup",
            parent_remote_id="g1",
            ad_type="REGULAR",
            name="ad-x0",
            configuration={},
            operation_status="ENABLE",
            review_status="APPROVED",
            delivery_status="DELIVERING",
            observed_at=NOW - timedelta(hours=1),
            published_version=1,
            source_connection_id=connection.id,
            source_channel="OFFICIAL_API",
        )
    )
    session.add(
        AdMaterialReference(
            tenant_id=env.tenant_id,
            advertiser_id="acc-x",
            ad_remote_id="x0",
            platform_material_id="v0",
            ad_material_id=None,
            material_type="VIDEO",
            name="素材一",
            main_material_id="m1",
            main_material_type="VIDEO",
            operation_status="ENABLE",
            complete=True,
            published_version=1,
        )
    )
    session.flush()
    target["material_uses"].insert(
        0,
        {
            "advertiser_id": "acc-x",
            "ad_remote_id": "x0",
            "platform_material_id": "v0",
            "ad_material_id": None,
            "material_type": "VIDEO",
        },
    )
    rule, run = _rule_run(
        session, env, action={"type": "PAUSE", "level": "material"}, targets=[target]
    )
    summary = execute_hits(
        session,
        run=run,
        rule=rule,
        selected_keys=None,
        now=NOW,
        actor_id=rule.updated_by,
        require_manage=True,
    )
    assert summary["submitted"] == 1 and summary["unsupported"] == 0
    assert _hits(session, run) == {"material:acc-x:m1": "SUBMITTED"}
    ledger = session.exec(
        select(AutomationActionLedger).where(AutomationActionLedger.run_id == run.id)
    ).all()
    assert [row.target_key for row in ledger] == ["material:acc-x:m1"]
