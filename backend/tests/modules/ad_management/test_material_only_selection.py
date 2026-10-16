"""广告详情只冻结素材，不把所属广告加入待修改对象。"""

import pytest
from sqlmodel import select

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.ads import EntityRef, MaterialUseRef
from app.modules.ad_management.expansion import expand_targets
from app.modules.ad_management.models import ManagementPreviewItem
from app.modules.ad_management.previews import prepare_preview
from app.modules.ad_management.schemas import MutationSpec
from app.modules.ads.models import AdMaterialReference
from app.modules.reporting.ad_materials import freeze_ad_material_selection
from tests.modules.ad_management.conftest import _row, _set_selection


@pytest.mark.parametrize("exclude_second", [False, True])
def test_detail_selection_only_targets_selected_materials(
    session, management_env, exclude_second
):
    context, bc, account, route, _ = management_env
    campaign = EntityRef(context.tenant_id, account.advertiser_id, "campaign", "series")
    ad = EntityRef(context.tenant_id, account.advertiser_id, "ad", "material-ad")
    session.add_all(
        [
            _row(campaign, connection_id=route.connection_id),
            _row(
                ad,
                parent=campaign,
                ad_type="SMART_PLUS",
                connection_id=route.connection_id,
            ),
            *[
                AdMaterialReference(
                    tenant_id=context.tenant_id,
                    advertiser_id=account.advertiser_id,
                    ad_remote_id=ad.remote_id,
                    platform_material_id=f"video-{index}",
                    ad_material_id=f"material-{index}",
                    material_type="VIDEO",
                    complete=True,
                    operation_status="ENABLE",
                    published_version=1,
                )
                for index in range(3)
            ],
        ]
    )
    session.flush()
    selection = freeze_ad_material_selection(
        session,
        context=context,
        bc_id=bc.bc_id,
        advertiser_id=account.advertiser_id,
        ad_id=ad.remote_id,
        ad_material_ids=("material-0", "material-1"),
    )
    assert not selection.refs
    mutation = MutationSpec(
        field="status",
        mode="set",
        value="DISABLE",
        excluded_material_uses=(selection.material_uses[1],) if exclude_second else (),
    )
    preview = prepare_preview(
        session, context, bc.bc_id, selection.selection_id, mutation
    )
    commands = expand_targets(session, context, selection.selection_id, mutation)
    expected = {"material-0"} if exclude_second else {"material-0", "material-1"}
    assert preview.counts.selected == preview.counts.targets == len(expected)
    assert {c.ad_material_id for c in commands} == expected
    assert all(c.operation == "set_material_status" and c.ref == ad for c in commands)
    stored = session.exec(
        select(ManagementPreviewItem).where(
            ManagementPreviewItem.preview_id == preview.preview_id,
            ManagementPreviewItem.execution_result == "PENDING",
        )
    ).all()
    assert len(stored) == len(expected)
    assert all(i.capability["operation"] == "set_material_status" for i in stored)


@pytest.mark.parametrize("wrong_source", [False, True])
def test_material_parent_still_requires_existing_scoped_source(
    session, management_env, wrong_source
):
    from app.modules.accounts.models import TikTokConnection

    context, bc, account, route, selection = management_env
    ad = EntityRef(context.tenant_id, account.advertiser_id, "ad", "unavailable-ad")
    use = MaterialUseRef(ad, "video", "material", "VIDEO")
    _set_selection(selection, [], [use])
    if wrong_source:
        other = TikTokConnection(tenant_id=context.tenant_id, status="ACTIVE")
        session.add(other)
        session.flush()
        session.add(_row(ad, connection_id=other.id))
    session.flush()
    if wrong_source:
        with pytest.raises(DomainError, match="来源"):
            prepare_preview(
                session,
                context,
                bc.bc_id,
                selection.id,
                MutationSpec(field="status", mode="set", value="DISABLE"),
            )
    else:
        preview = prepare_preview(
            session,
            context,
            bc.bc_id,
            selection.id,
            MutationSpec(field="status", mode="set", value="DISABLE"),
        )
        assert preview.counts.targets == 0
        assert preview.items[0].reason == "ad_not_found"
