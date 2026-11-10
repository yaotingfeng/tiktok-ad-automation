"""创意日事实的规则输入：共享视频按广告隔离，缺口不执行。"""

from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

from app.modules.ads.creatives import upsert_creatives
from app.modules.ads.models import AdMaterialReference
from app.modules.automation_rules.inputs import AccountInfo, _materials, _objects
from app.modules.automation_rules.material_inputs import material_facts
from app.modules.reporting.publish import (
    AccountMeta,
    publish_ad_day,
    publish_creative_day,
)
from tests.modules.automation_rules.test_inputs import NOW, TODAY, _seed, definition
from tests.modules.reporting.sync_conftest import ad_row


def prepare(session, seed, *, residual=False, unmapped=False):
    _seed(session, seed)
    accounts = {
        "acc-1": AccountInfo("acc-1", "bc-a", "Asia/Hong_Kong", "USD", "account")
    }
    ads = _objects(session, seed.tenant_id, accounts, NOW)
    ads = {k: replace(v, status="ENABLE") for k, v in ads.items()}
    session.add(
        AdMaterialReference(
            tenant_id=seed.tenant_id,
            advertiser_id="acc-1",
            ad_remote_id="a1",
            platform_material_id="v1",
            ad_material_id="am1",
            material_type="VIDEO",
            operation_status="ENABLE",
            complete=True,
            published_version=1,
        )
    )
    upsert_creatives(
        session,
        tenant_id=seed.tenant_id,
        advertiser_id="acc-1",
        rows=[
            {
                "creative_id": "cr1",
                "ad_remote_id": "a1",
                "ad_material_id": "am1",
                "assets": [{"material_type": "VIDEO", "platform_material_id": "v1"}],
            }
        ],
        observed_at=NOW,
        published_version=1,
    )
    args = {
        "tenant_id": seed.tenant_id,
        "job_id": uuid4(),
        "accounts": {"acc-1": AccountMeta("USD", "Asia/Hong_Kong")},
        "start": TODAY,
        "end": TODAY,
    }
    ar = ad_row(
        "a1", TODAY, campaign="c1", adgroup="g1", spend="31" if residual else "30"
    )
    ar["metrics"]["native_growth_total_ad_impression_value"] = "15"
    cr = ad_row(
        "missing" if unmapped else "cr1", TODAY, campaign="c1", adgroup="g1", spend="30"
    )
    cr["dimensions"]["ad_id"] = cr["dimensions"].pop("ad_id_v2")
    cr["metrics"]["native_growth_total_ad_impression_value"] = "15"
    publish_ad_day(session, **args, rows=[ar])
    publish_creative_day(session, **args, rows=[cr])
    from app.modules.reporting.store_models import ReportSyncState

    for kind in ("ad_day", "creative_day"):
        state = session.get(ReportSyncState, (seed.tenant_id, "acc-1", kind, TODAY))
        state.observed_at = NOW
        state.last_success_at = NOW
    session.flush()
    rule = definition(
        apply_to={"level": "material", "status": "ACTIVE"},
        action={"type": "PAUSE", "level": "material"},
        conditions=[
            {"metric": "spend", "range": "TODAY", "operator": "GT", "values": ["20"]}
        ],
    )
    return _materials(session, seed.tenant_id, accounts, ads, definition=rule, now=NOW)


def test_creative_identity_replaces_post_link(session, sync_seed):
    entities = prepare(session, sync_seed)
    entity = next(iter(entities.values()))
    assert entity.report_ids == ("cr1",)
    assert entity.judgement_issue is None
    facts = material_facts(entities, {"creative:acc-1:cr1": {"spend": Decimal("30")}})
    assert facts[entity.key]["spend"] == Decimal("30")


def test_group_residual_blocks_material_action(session, sync_seed):
    entities = prepare(session, sync_seed, residual=True)
    assert (
        next(iter(entities.values())).judgement_issue == "MATERIAL_RECONCILIATION_GAP"
    )


def test_unmapped_creative_blocks_affected_group(session, sync_seed):
    entities = prepare(session, sync_seed, unmapped=True)
    assert (
        next(iter(entities.values())).judgement_issue == "MATERIAL_CREATIVE_UNRESOLVED"
    )
