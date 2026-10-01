from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.ads import EntityRef, MaterialUseRef
from app.modules.accounts.management_capability_models import ManagementCapability
from app.modules.ad_management.previews import prepare_preview
from app.modules.ad_management.schemas import MutationSpec
from app.modules.ads.models import AdObject


def _row(ref, *, parent=None, ad_type="REGULAR", configuration=None, status="ENABLE", connection_id=None):
    return AdObject(
        tenant_id=ref.tenant_id,
        advertiser_id=ref.advertiser_id,
        kind=ref.kind,
        remote_id=ref.remote_id,
        parent_kind=parent.kind if parent else None,
        parent_remote_id=parent.remote_id if parent else None,
        ad_type=ad_type,
        name=f"object-{ref.remote_id}",
        configuration=configuration or {},
        operation_status=status,
        review_status="APPROVED",
        delivery_status="DELIVERING",
        observed_at=datetime.now(UTC),
        published_version=1,
        source_connection_id=connection_id,
        source_channel="OFFICIAL_API",
    )


def _cap(session, context, bc, route, advertiser, operation, kind):
    session.add(
        ManagementCapability(
            tenant_id=context.tenant_id,
            bc_id=bc.bc_id,
            advertiser_id=advertiser,
            connection_id=route.connection_id,
            authorization_revision=route.authorization_revision,
            binding_revision=route.binding_revision,
            adapter_contract_revision=route.adapter_contract_revision,
            operation=operation,
            entity_kind=kind,
            state="VERIFIED",
            verified_at=datetime.now(UTC),
            evidence={"role": "ADMIN", "scope": ["management"]},
        )
    )


def _set_selection(selection, refs, uses=()):
    selection.refs = [
        {"tenant_id": str(ref.tenant_id), "advertiser_id": ref.advertiser_id, "kind": ref.kind, "remote_id": ref.remote_id}
        for ref in refs
    ]
    selection.material_uses = [
        {
            "ad_ref": {"tenant_id": str(use.ad_ref.tenant_id), "advertiser_id": use.ad_ref.advertiser_id, "kind": use.ad_ref.kind, "remote_id": use.ad_ref.remote_id},
            "platform_material_id": use.platform_material_id,
            "ad_material_id": use.ad_material_id,
            "material_type": use.material_type,
        }
        for use in uses
    ]
    selection.expires_at = datetime.now(UTC) + timedelta(minutes=15)


def test_percentage_and_budget_owner_previews(session, management_env):
    context, bc, account, route, selection = management_env
    campaign = EntityRef(context.tenant_id, account.advertiser_id, "campaign", "series-1")
    group_a = EntityRef(context.tenant_id, account.advertiser_id, "adgroup", "group-a")
    group_b = EntityRef(context.tenant_id, account.advertiser_id, "adgroup", "group-b")
    session.add_all([
        _row(campaign, configuration={"budget": "100"}, connection_id=route.connection_id),
        _row(group_a, parent=campaign, configuration={"roas_bid": "1.20", "budget_mode": "CAMPAIGN"}, connection_id=route.connection_id),
        _row(group_b, parent=campaign, configuration={"roas_bid": "1.20", "budget_mode": "CAMPAIGN"}, connection_id=route.connection_id),
    ])
    _cap(session, context, bc, route, account.advertiser_id, "update_roas", "adgroup")
    _cap(session, context, bc, route, account.advertiser_id, "update_budget", "campaign")
    session.flush()
    _set_selection(selection, [group_a])
    p = prepare_preview(session, context, bc.bc_id, selection.id, MutationSpec(field="roas", mode="increase_percent", value=10))
    assert (p.expires_at - p.created_at).total_seconds() == 300
    assert p.items[0].original_value == Decimal("1.20")
    assert p.items[0].final_value == Decimal("1.32")
    _set_selection(selection, [group_a, group_b])
    q = prepare_preview(session, context, bc.bc_id, selection.id, MutationSpec(field="budget", mode="set", value=Decimal("120")))
    assert q.counts.selected == 2 and q.counts.targets == 1
    assert q.items[0].ref.kind == "campaign"


def test_ordinary_material_without_ad_reference_is_unsupported(session, management_env):
    context, bc, account, route, selection = management_env
    ad = EntityRef(context.tenant_id, account.advertiser_id, "ad", "ad-ordinary")
    use = MaterialUseRef(ad, "platform-vid", None, "VIDEO")
    session.add(_row(ad, ad_type="REGULAR", configuration={"budget": "10"}, connection_id=route.connection_id))
    _cap(session, context, bc, route, account.advertiser_id, "set_material_status", "ad")
    session.flush()
    _set_selection(selection, [ad], [use])
    p = prepare_preview(session, context, bc.bc_id, selection.id, MutationSpec(field="status", mode="set", value="DISABLE"))
    assert p.items[0].material_use.ad_material_id is None
    assert p.items[0].execution_result == "UNSUPPORTED"
    assert p.counts.targets == 0


def test_same_series_divergent_roas_rejected(session, management_env):
    context, bc, account, route, selection = management_env
    campaign = EntityRef(context.tenant_id, account.advertiser_id, "campaign", "series-2")
    a = EntityRef(context.tenant_id, account.advertiser_id, "adgroup", "group-c")
    b = EntityRef(context.tenant_id, account.advertiser_id, "adgroup", "group-d")
    session.add_all([
        _row(campaign, connection_id=route.connection_id),
        _row(a, parent=campaign, configuration={"roas_bid": "1.20"}, connection_id=route.connection_id),
        _row(b, parent=campaign, configuration={"roas_bid": "1.30"}, connection_id=route.connection_id),
    ])
    _cap(session, context, bc, route, account.advertiser_id, "update_roas", "adgroup")
    session.flush()
    _set_selection(selection, [a, b])
    with pytest.raises(DomainError, match="最终 ROAS"):
        prepare_preview(session, context, bc.bc_id, selection.id, MutationSpec(field="roas", mode="increase_percent", value=10))
