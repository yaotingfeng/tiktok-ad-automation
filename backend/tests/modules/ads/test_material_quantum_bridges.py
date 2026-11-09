"""真实PG量子只索引一次冻结桥；索引不能越过提交或跨量子复用。"""

import pytest

from app.modules.ads import tasks
from tests.modules.ads.test_material_quantum_context import (
    app_config as app_config,
)
from tests.modules.ads.test_material_quantum_context import (
    database_engine as database_engine,
)
from tests.modules.ads.test_material_quantum_context import (
    durable_material_case as durable_material_case,
)
from tests.modules.ads.test_material_quantum_context import (
    gateway_case as gateway_case,
)
from tests.modules.ads.test_material_quantum_context import (
    gateway_wire as gateway_wire,
)
from tests.modules.ads.test_material_quantum_context import (
    material_case as material_case,
)
from tests.modules.ads.test_material_quantum_context import (
    policy as policy,
)


def _count_material_walks(monkeypatch):
    scopes = []
    original = tasks.material_child_scope

    class Materials(tuple):
        walks = 0

        def __iter__(self):
            self.walks += 1
            return super().__iter__()

    def scope(*args, **kwargs):
        value = original(*args, **kwargs)
        if value is None:
            return None
        owners, materials = value
        measured = Materials(materials)
        scopes.append(measured)
        return owners, measured

    monkeypatch.setattr(tasks, "material_child_scope", scope)
    return scopes


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
def test_five_page_quantum_walks_material_bridges_once(material_case, monkeypatch):
    scopes = _count_material_walks(monkeypatch)
    case = material_case
    generation = case.claim(case.child_id)
    assert case.run(case.child_id, generation) == "CONTINUE"
    assert len(scopes) == 1
    assert scopes[0].walks == 1


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
@pytest.mark.parametrize("material_case", [6002], indirect=True)
def test_new_quantum_builds_its_own_material_index(material_case, monkeypatch):
    scopes = _count_material_walks(monkeypatch)
    case = material_case
    for _ in range(2):
        assert case.run(case.child_id, case.claim(case.child_id)) == "CONTINUE"
    assert len(scopes) == 2
    assert [scope.walks for scope in scopes] == [1, 1]


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
def test_index_is_prepared_after_claim_commit_and_session_exit(
    material_case, monkeypatch
):
    from contextlib import contextmanager

    from sqlmodel import Session

    from app.modules.ads.sync_models import AdDirectoryRun

    case = material_case
    original_session = tasks._directory_session
    original_prepare = tasks.prepare_material_bridges
    depth = 0
    prepared = 0

    @contextmanager
    def tracked(*args, **kwargs):
        nonlocal depth
        with original_session(*args, **kwargs) as session:
            depth += 1
            try:
                yield session
            finally:
                depth -= 1

    def prepare(*args):
        nonlocal prepared
        assert depth == 0
        # 独立真实连接必须看到已提交的claim，而非只检查Python调用顺序。
        with Session(case.engine) as session:
            run = session.get(AdDirectoryRun, case.child_id)
            assert run.claim_token is not None and run.claimed_until is not None
        prepared += 1
        return original_prepare(*args)

    monkeypatch.setattr(tasks, "_directory_session", tracked)
    monkeypatch.setattr(tasks, "prepare_material_bridges", prepare)
    assert case.run(case.child_id, case.claim(case.child_id)) == "CONTINUE"
    assert prepared == 1


@pytest.fixture
def bridge_case(gateway_case):
    from datetime import UTC, datetime

    from app.integrations.tiktok.contracts.ads import (
        AdEntity,
        AdMaterialUsage,
        EntityRef,
        MaterialUseRef,
    )

    context, _, advertiser = gateway_case
    ref = EntityRef(context.tenant_id, advertiser, "ad", "ad-a")
    owner = AdEntity(
        ref,
        EntityRef(context.tenant_id, advertiser, "adgroup", "group"),
        "SMART_PLUS",
        "",
        {},
        None,
        None,
        None,
        datetime.now(UTC),
    )
    material = AdMaterialUsage(
        MaterialUseRef(ref, "video", "native", "VIDEO"),
        None,
        "DISABLE",
        False,
        "original",
        ("creative",),
    )
    return (owner,), (material,)


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
@pytest.mark.parametrize(
    "change",
    ["ads_tuple", "materials_tuple", "owners", "tenant", "account", "foreign_tail"],
)
def test_prepared_scope_rejected_before_http(
    database_engine, redis_client, gateway_case, gateway_wire, bridge_case, change
):
    from dataclasses import replace
    from uuid import uuid4

    from app.core.errors import DomainError
    from app.integrations.tiktok.adapters.ads_read import prepare_material_bridges
    from tests.integrations.tiktok.gateway_support import business_calls, gateway
    from tests.integrations.tiktok.test_ads_read_adapters import query

    ads, materials = bridge_case
    prepared = prepare_material_bridges(ads, materials)
    if change == "ads_tuple":
        ads = tuple(list(ads))  # noqa: C414 - 必须建立值相等但身份不同的源元组
    elif change == "materials_tuple":
        materials = tuple(list(materials))  # noqa: C414 - 验证身份而非值相等
    elif change == "owners":
        with pytest.raises(TypeError, match="init=False"):
            replace(prepared, owners=frozenset())
        return
    elif change in {"tenant", "account"}:
        scope = (
            {"tenant_id": uuid4()}
            if change == "tenant"
            else {"advertiser_id": "foreign"}
        )
        owner = replace(
            ads[0],
            ref=replace(ads[0].ref, **scope),
            parent_ref=replace(ads[0].parent_ref, **scope),
        )
        ads = (owner,)
        materials = (
            replace(
                materials[0], use_ref=replace(materials[0].use_ref, ad_ref=owner.ref)
            ),
        )
        prepared = prepare_material_bridges(ads, materials)
    else:
        foreign = replace(
            materials[0],
            use_ref=replace(
                materials[0].use_ref, ad_ref=replace(ads[0].ref, remote_id="outside")
            ),
        )
        with pytest.raises(DomainError):
            prepare_material_bridges(ads, materials + (foreign,))
        return
    with gateway(database_engine, redis_client, gateway_case) as client:
        with pytest.raises(DomainError) as error:
            client.ads.read_material_page(
                query(gateway_case, "creative", "SMART_PLUS"),
                ads=ads,
                materials=materials,
                prepared=prepared,
            )
        assert error.value.code == "directory_scope_mismatch"
    assert not business_calls(gateway_wire, gateway_case[1].channel)


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize("conflict", [None, "ambiguous", "native"])
def test_prepared_exact_bridge_status_and_conflict(
    database_engine, redis_client, gateway_case, gateway_wire, bridge_case, conflict
):
    from dataclasses import replace

    from app.core.errors import DomainError
    from app.integrations.tiktok.adapters.ads_read import prepare_material_bridges
    from tests.integrations.tiktok.gateway_support import gateway
    from tests.integrations.tiktok.test_ads_read_adapters import page, query, replies

    ads, materials = bridge_case
    if conflict == "ambiguous":
        materials += (
            replace(
                materials[0],
                use_ref=replace(materials[0].use_ref, ad_material_id="another"),
            ),
        )
    prepared = prepare_material_bridges(ads, materials)
    key = next(iter(prepared.entries))
    with pytest.raises(TypeError):
        prepared.entries[key] = {}
    with pytest.raises(TypeError):
        prepared.entries[key][materials[0].use_ref] = materials[0]
    row = {
        "advertiser_id": gateway_case[2],
        "adgroup_id": "group",
        "smart_plus_ad_id": "ad-a",
        "ad_id": "creative",
        "video_id": "video",
        "operation_status": "ENABLE",
    }
    if conflict == "native":
        row["ad_material_id"] = "conflicting"
    replies(gateway_wire, gateway_case[1].channel, {"ad_get": [page([row], size=1000)]})
    child_query = replace(query(gateway_case, "creative", "SMART_PLUS"), page_size=1000)
    with gateway(database_engine, redis_client, gateway_case) as client:
        if conflict:
            with pytest.raises(DomainError):
                client.ads.read_material_page(
                    child_query, ads=ads, materials=materials, prepared=prepared
                )
        else:
            result = client.ads.read_material_page(
                child_query, ads=ads, materials=materials, prepared=prepared
            )
            assert result.materials[0].use_ref.ad_material_id == "native"
            assert result.materials[0].operation_status == "DISABLE"
            assert result.materials[0].name == "original"
            assert result.items[0].operation_status == "ENABLE"


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
def test_derived_index_cannot_be_forged_or_hide_foreign_tail(bridge_case):
    from dataclasses import replace

    from app.core.errors import DomainError
    from app.integrations.tiktok.contracts.ads import PreparedMaterialBridges

    ads, materials = bridge_case
    # 即使两个native ID共用桥，调用方也不能通过注入空entries隐藏歧义。
    ambiguous = materials + (
        replace(
            materials[0],
            use_ref=replace(materials[0].use_ref, ad_material_id="another"),
        ),
    )
    prepared = PreparedMaterialBridges(ads, ambiguous)
    assert len(next(iter(prepared.entries.values()))) == 2
    with pytest.raises(TypeError):
        PreparedMaterialBridges(ads, ambiguous, entries={})
    with pytest.raises(TypeError, match="init=False"):
        replace(prepared, entries={})
    foreign = replace(
        materials[0],
        use_ref=replace(
            materials[0].use_ref, ad_ref=replace(ads[0].ref, remote_id="outside")
        ),
    )
    invalid_materials = materials + (foreign,)
    with pytest.raises(DomainError):
        PreparedMaterialBridges(ads, invalid_materials)
    with pytest.raises(DomainError):
        replace(prepared, materials=invalid_materials)


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
@pytest.mark.parametrize("material_case", [6002], indirect=True)
def test_each_quantum_constructs_frozen_material_dtos_once(material_case, monkeypatch):
    from app.modules.ads import material_directory_children

    case = material_case
    original = material_directory_children._usage
    constructed = 0

    def usage(value):
        nonlocal constructed
        constructed += 1
        return original(value)

    monkeypatch.setattr(material_directory_children, "_usage", usage)
    # Fixture冻结两个主响应素材桥；6002条远端结果分为5+2页两个量子。
    for quantum in (1, 2):
        assert case.run(case.child_id, case.claim(case.child_id)) == "CONTINUE"
        assert constructed == 2 * quantum


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
def test_terminal_read_and_publisher_do_not_retain_frozen_materials(
    material_case, monkeypatch
):
    from app.modules.ads import material_directory_children
    from tests.integrations.tiktok.gateway_support import business_calls

    case = material_case
    generation = case.claim(case.child_id)
    assert case.run(case.child_id, generation) == "CONTINUE"
    calls = len(business_calls(case.wire, "OFFICIAL_API"))
    original = material_directory_children.material_child_scope
    modes = []

    def checked(*args, **kwargs):
        modes.append(kwargs.get("include_materials", True))
        scope = original(*args, **kwargs)
        assert not scope[1]
        return scope

    def unexpected(*_args, **_kwargs):
        pytest.fail("terminal/publisher must not prepare retained material scope")

    monkeypatch.setattr(material_directory_children, "material_child_scope", checked)
    monkeypatch.setattr(tasks, "material_child_scope", unexpected)
    assert case.run(case.child_id, generation) == "WAIT"
    assert modes == []
    assert case.run(case.child_id, generation, publish_only=True) == "READY"
    assert modes and not any(modes)
    assert len(business_calls(case.wire, "OFFICIAL_API")) == calls


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
@pytest.mark.parametrize("stale", [False, True])
def test_entry_validates_foreign_tail_before_stale_or_http(material_case, stale):
    from copy import deepcopy
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from sqlmodel import Session, select

    from app.core.errors import DomainError
    from app.modules.ads.material_directory_children import MATERIAL_CONTEXT_PAGE
    from app.modules.ads.models import AdObject
    from app.modules.ads.sync_models import AdDirectoryPage
    from tests.integrations.tiktok.gateway_support import business_calls

    case = material_case
    with Session(case.engine) as db, db.begin():
        context = db.get(AdDirectoryPage, (case.child_id, MATERIAL_CONTEXT_PAGE))
        evidence = deepcopy(context.evidence)
        materials = evidence["material_child_v1"]["materials"]
        evidence["material_child_v1"]["materials"] = [
            deepcopy(materials[0]) for _ in range(501)
        ] + [deepcopy(materials[-1])]
        evidence["material_child_v1"]["materials"][-1]["use_ref"]["ad_ref"][
            "tenant_id"
        ] = str(uuid4())
        context.evidence = evidence
        db.add(context)
        if stale:
            for ad in db.exec(
                select(AdObject).where(AdObject.tenant_id == case.context.tenant_id)
            ).all():
                ad.observed_at = datetime.now(UTC) + timedelta(seconds=1)
                db.add(ad)
    generation = case.claim(case.child_id)
    with pytest.raises(DomainError) as error:
        case.run(case.child_id, generation)
    assert error.value.code == "directory_scope_mismatch"
    assert len(business_calls(case.wire, "OFFICIAL_API")) == 1
