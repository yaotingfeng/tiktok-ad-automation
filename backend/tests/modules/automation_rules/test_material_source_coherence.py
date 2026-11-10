"""真实双连接提交插入门控与金额读取之间，不借隔离级别掩盖调用方事务。"""

from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlmodel import Session

from app.core.db import engine
from app.modules.automation_rules import inputs, material_inputs
from app.modules.reporting.store_models import ReportSyncState
from tests.modules.automation_rules.test_creative_material_inputs import prepare
from tests.modules.automation_rules.test_inputs import NOW, TODAY, definition
from tests.modules.reporting.sync_conftest import (
    committed_sync_seed as committed_sync_seed,
)
from tests.modules.reporting.sync_conftest import sync_seed


@pytest.fixture
def committed_material_case():
    with Session(engine, expire_on_commit=False) as setup:
        seed = sync_seed.__wrapped__(setup)
        prepare(setup, seed)
        # 第二窗口为已证实完整空桶；不能只校验有金额的行。
        for kind in ("ad_day", "creative_day"):
            state = setup.get(
                ReportSyncState,
                (seed.tenant_id, "acc-1", kind, TODAY - timedelta(days=1)),
            )
            if state is None:
                state = ReportSyncState(
                    tenant_id=seed.tenant_id,
                    advertiser_id="acc-1",
                    report_kind=kind,
                    stat_date=TODAY - timedelta(days=1),
                )
            state.status = "COMPLETE"
            state.observed_at = state.last_success_at = NOW
            setup.add(state)
        setup.connection().execute(
            text("UPDATE ad_object SET operation_status='ENABLE' WHERE tenant_id=:t"),
            {"t": seed.tenant_id},
        )
        setup.commit()
    yield seed
    # 所有已提交测试数据均由账户/租户外键级联清理；运行只使用专用本地 PG。
    with engine.begin() as conn:
        for table in (
            "campaign_name_projection",
            "ad_material_reference",
            "ad_creative",
            "ad_object",
            "discovery_run",
            "connection_authorization",
            "bc_connection_binding",
            "bc_account_access",
            "advertiser_account",
            "tenant_bc",
            "tiktok_connection",
            "tenant_membership",
        ):
            conn.execute(
                text(f"DELETE FROM {table} WHERE tenant_id=:t"), {"t": seed.tenant_id}
            )
        conn.execute(text("DELETE FROM tenant WHERE id=:t"), {"t": seed.tenant_id})
        conn.execute(text('DELETE FROM "user" WHERE id=:u'), {"u": seed.actor_id})


@pytest.mark.parametrize(
    "mutation",
    [
        "revenue",
        "publication",
        "identity",
        "empty",
        "missing",
        "usage",
        "delete-usage",
        "identity-no-facts",
        "unchanged",
    ],
)
def test_load_source_change_is_audited_and_never_executable(
    committed_material_case, monkeypatch, mutation
):
    seed = committed_material_case
    rule = definition(
        apply_to={"level": "material", "status": "ACTIVE"},
        action={"type": "PAUSE", "level": "material"},
        conditions=[
            {
                "metric": "roas_total",
                "range": "TODAY",
                "operator": "LT",
                "values": ["1"],
            },
            {
                "metric": "spend",
                "range": "YESTERDAY",
                "operator": "LT",
                "values": ["1"],
            },
        ],
    )
    if mutation == "identity-no-facts":
        with Session(engine) as writer:
            for table in ("ad_report_day", "creative_report_day"):
                writer.connection().execute(
                    text(f"DELETE FROM {table} WHERE tenant_id=:t AND stat_date=:d"),
                    {"t": seed.tenant_id, "d": TODAY},
                )
            writer.commit()
    gate = material_inputs.group_issues

    def interleave(*args, **kwargs):
        result = gate(*args, **kwargs)
        assert not result
        with Session(engine) as writer:
            sql = {
                "publication": "SELECT 1",
                "revenue": "UPDATE creative_report_day SET revenue_total=1 WHERE tenant_id=:t AND stat_date=:d",
                "identity": "UPDATE ad_creative SET identity_status='CONFLICT' WHERE tenant_id=:t",
                "delete-usage": "DELETE FROM ad_material_reference WHERE tenant_id=:t",
                "identity-no-facts": "UPDATE ad_creative SET identity_status='CONFLICT' WHERE tenant_id=:t",
                "usage": "UPDATE ad_material_reference SET ad_material_id='replaced' WHERE tenant_id=:t",
                "empty": "UPDATE report_sync_state SET status='FAILED' WHERE tenant_id=:t AND stat_date=:y AND report_kind='creative_day'",
                "missing": "DELETE FROM creative_report_day WHERE tenant_id=:t AND stat_date=:d",
                "unchanged": "SELECT 1",
            }[mutation]
            writer.connection().execute(
                text(sql),
                {"t": seed.tenant_id, "d": TODAY, "y": TODAY - timedelta(days=1)},
            )
            if mutation == "publication":
                from uuid import uuid4

                from app.modules.reporting.publish import (
                    AccountMeta,
                    publish_creative_day,
                )
                from tests.modules.reporting.sync_conftest import ad_row

                row = ad_row(
                    "cr1", TODAY, campaign="c1", adgroup="g1", spend="30", total="1"
                )
                row["dimensions"]["ad_id"] = row["dimensions"].pop("ad_id_v2")
                publish_creative_day(
                    writer,
                    tenant_id=seed.tenant_id,
                    job_id=uuid4(),
                    accounts={"acc-1": AccountMeta("USD", "Asia/Hong_Kong")},
                    start=TODAY,
                    end=TODAY,
                    rows=[row],
                )
            writer.commit()
        return result

    monkeypatch.setattr(material_inputs, "group_issues", interleave)
    with Session(engine) as reader, reader.begin_nested():
        # 调用方已经开始事务，新增保护仍必须可用。
        reader.connection().execute(text("SELECT 1"))
        loaded = inputs.load_inputs(
            reader, tenant_id=seed.tenant_id, definition=rule, now=NOW
        )
    if mutation == "delete-usage":
        assert not any(e.kind == "material" for e in loaded.entities.values())
        assert any(
            s.get("reason") == "MATERIAL_SOURCE_CHANGED"
            for s in loaded.skipped_accounts
        )
        return
    material = next(e for e in loaded.entities.values() if e.kind == "material")
    from app.modules.automation_rules.evaluate import evaluate

    decision = evaluate(rule, loaded)
    if mutation == "unchanged":
        assert len(decision.hits) == 1
        assert material.judgement_issue is None
        assert loaded.facts["TODAY"][material.key]["revenue_total"] == 15
    else:
        assert not decision.hits
        assert material.key in decision.unknown_targets
        assert material.judgement_issue == "MATERIAL_SOURCE_CHANGED"
        assert all(material.key not in facts for facts in loaded.facts.values())
        assert any(
            s.get("reason") == "MATERIAL_SOURCE_CHANGED"
            for s in loaded.skipped_accounts
        )


def test_changes_between_required_windows_invalidate_entire_load(
    committed_material_case, monkeypatch
):
    seed = committed_material_case
    rule = definition(
        apply_to={"level": "material", "status": "ACTIVE"},
        action={"type": "PAUSE", "level": "material"},
        conditions=[
            {"metric": "spend", "range": r, "operator": "LT", "values": ["100"]}
            for r in ("TODAY", "YESTERDAY")
        ],
    )
    original = inputs._facts
    calls = []

    def interleave(*args, **kwargs):
        value = original(*args, **kwargs)
        calls.append(args[3])
        if len(calls) == 1:
            with engine.begin() as writer:
                writer.execute(
                    text(
                        "UPDATE report_sync_state SET status='FAILED' WHERE tenant_id=:t AND stat_date=:d AND report_kind='creative_day'"
                    ),
                    {"t": seed.tenant_id, "d": TODAY - timedelta(days=1)},
                )
        return value

    monkeypatch.setattr(inputs, "_facts", interleave)
    with Session(engine) as reader:
        result = inputs.load_inputs(
            reader, tenant_id=seed.tenant_id, definition=rule, now=NOW
        )
    assert len(calls) == 2
    assert all(not facts for facts in result.facts.values())
    assert any(
        s.get("reason") == "MATERIAL_SOURCE_CHANGED" for s in result.skipped_accounts
    )


def test_reused_session_refreshes_identity_before_stable_proof(committed_material_case):
    from app.modules.ads.creative_models import AdCreative

    seed = committed_material_case
    rule = definition(
        apply_to={"level": "material", "status": "ACTIVE"},
        action={"type": "PAUSE", "level": "material"},
    )
    with Session(engine) as reader:
        cached = reader.get(AdCreative, (seed.tenant_id, "acc-1", "cr1"))
        with engine.begin() as writer:
            writer.execute(
                text(
                    "UPDATE ad_creative SET identity_status='CONFLICT' WHERE tenant_id=:t"
                ),
                {"t": seed.tenant_id},
            )
        result = inputs.load_inputs(
            reader, tenant_id=seed.tenant_id, definition=rule, now=NOW
        )
        assert cached.identity_status == "CONFLICT"
        assert all(
            e.judgement_issue for e in result.entities.values() if e.kind == "material"
        )


def test_first_empty_publication_changes_missing_bucket_proof(committed_sync_seed):
    from uuid import uuid4

    from app.modules.automation_rules.inputs import AccountInfo
    from app.modules.automation_rules.material_source import source_proof
    from app.modules.reporting.publish import AccountMeta, publish_creative_day

    seed, factory = committed_sync_seed
    accounts = {
        adv: AccountInfo(adv, "bc-a", "UTC", "USD", adv) for adv in ("acc-1", "acc-2")
    }
    with factory() as reader:
        before = source_proof(
            reader, seed.tenant_id, accounts, ["TODAY", "YESTERDAY"], NOW
        )
        with factory() as publisher:
            publish_creative_day(
                publisher,
                tenant_id=seed.tenant_id,
                job_id=uuid4(),
                accounts={"acc-1": AccountMeta("USD", "UTC")},
                start=TODAY - timedelta(days=1),
                end=TODAY,
                rows=[],
            )
            publisher.commit()
        after = source_proof(
            reader, seed.tenant_id, accounts, ["TODAY", "YESTERDAY"], NOW
        )
    assert before["acc-1"] != after["acc-1"]
    assert before["acc-2"] == after["acc-2"]
