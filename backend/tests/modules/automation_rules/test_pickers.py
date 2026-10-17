"""编辑器中可搜索的账户与对象选择器。"""

from app.modules.ads.models import AdMaterialReference
from app.modules.automation_rules.pickers import search_accounts, search_objects
from tests.modules.automation_rules.conftest import headers
from tests.modules.automation_rules.test_inputs import _seed


def test_account_search_by_id_or_name(session, sync_seed, other_context):
    found = search_accounts(
        session, context=sync_seed.context, query="账户二", limit=50
    )
    assert [item.advertiser_id for item in found] == ["acc-2"]
    assert found[0].bc_id == "bc-a" and found[0].name == "账户二"
    assert [
        item.advertiser_id
        for item in search_accounts(
            session, context=sync_seed.context, query="acc", limit=1
        )
    ] == ["acc-1"]
    assert search_accounts(session, context=other_context, query="", limit=50) == []


def test_object_search_levels_and_materials(session, sync_seed):
    _seed(session, sync_seed)
    groups = search_objects(
        session,
        context=sync_seed.context,
        level="adgroup",
        query="组一",
        advertiser_ids=[],
        limit=50,
    )
    assert [(item.value, item.label) for item in groups] == [("acc-1:g1", "组一")]
    for remote in ("x1", "x2"):
        session.add(
            AdMaterialReference(
                tenant_id=sync_seed.tenant_id,
                advertiser_id="acc-1",
                ad_remote_id="a1",
                platform_material_id=f"v-{remote}",
                ad_material_id=f"am-{remote}",
                complete=True,
                material_type="VIDEO",
                name="素材甲",
                main_material_id="m1",
                main_material_type="VIDEO",
                published_version=1,
            )
        )
    session.flush()
    materials = search_objects(
        session,
        context=sync_seed.context,
        level="material",
        query="素材",
        advertiser_ids=["acc-1"],
        limit=50,
    )
    # 指定对象与判断引擎一致：每个广告内视频独立选择，不依赖目录主素材字段。
    assert len(materials) == 2 and materials[0].value != materials[1].value
    assert all(item.label == "素材甲 · 广告一" for item in materials)
    assert (
        search_objects(
            session,
            context=sync_seed.context,
            level="adgroup",
            query="",
            advertiser_ids=["acc-2"],
            limit=50,
        )[0].advertiser_id
        == "acc-2"
    )


def test_picker_endpoints(client, sync_seed, other_context):
    base = f"/api/tenants/{sync_seed.tenant_id}/automation-rules/pickers"
    response = client.get(
        f"{base}/accounts", params={"q": "acc"}, headers=headers(sync_seed.context)
    )
    assert response.status_code == 200 and len(response.json()) == 2
    response = client.get(
        f"{base}/objects",
        params={"level": "campaign", "q": ""},
        headers=headers(sync_seed.context),
    )
    assert response.status_code == 200
    other = client.get(
        f"/api/tenants/{sync_seed.tenant_id}/automation-rules/pickers/accounts",
        headers=headers(other_context),
    )
    assert other.status_code in (403, 404)


def test_object_list_pages_and_shows_status(session, sync_seed):
    _seed(session, sync_seed)
    first = search_objects(
        session,
        context=sync_seed.context,
        level="campaign",
        query="",
        advertiser_ids=[],
        limit=1,
        offset=0,
    )
    second = search_objects(
        session,
        context=sync_seed.context,
        level="campaign",
        query="",
        advertiser_ids=[],
        limit=1,
        offset=1,
    )
    # 勾选列表分页加载，不同页不重复；每行带状态供用户辨认。
    assert len(first) == 1 and len(second) == 1 and first[0].value != second[0].value
    assert first[0].status in {"ENABLE", "DISABLE"}
