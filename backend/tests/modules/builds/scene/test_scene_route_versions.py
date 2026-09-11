"""场景固定授权语义；凭据材料和目录观察更新不能改变原任务。"""

import inspect
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlmodel import Session, select

from app.core.errors import DomainError
from app.modules.accounts.connection_models import (
    BCDefaultRoute,
    ConnectionAuthorization,
)
from app.modules.accounts.models import BCAccountAccess, TikTokConnection
from app.modules.builds.scene import _scope
from tests.modules.builds.scene.support import ensure


@pytest.mark.parametrize("name", ["read_scene_context", "ensure_scene_preparation"])
def test_scene_entry_requires_an_explicit_frozen_route(name):
    from app.modules.builds import scene, scene_jobs

    function = getattr(scene if name == "read_scene_context" else scene_jobs, name)
    parameter = inspect.signature(function).parameters.get("route")
    assert parameter is not None, "场景入口必须消费父冻结路由"
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty


def test_scene_records_have_a_nullable_historical_route_column():
    from app.modules.builds.scene_job_models import DraftScenePreparation, SceneJob

    for model in (SceneJob, DraftScenePreparation):
        column = model.__table__.columns.get("frozen_route")
        assert column is not None, "任务必须持久化原冻结路由"
        assert column.nullable, "旧任务不能伪造今日授权"


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize("change", ["credential", "observed", "default"])
def test_same_scene_basis_survives_nonsemantic_updates(
    database_engine, scene_case, change
):
    case = scene_case
    route = case["route"]
    kwargs = {
        "context": case["context"],
        "bc_id": route.bc_id,
        "advertiser_id": case["advertiser_id"],
        "link_id": case["link_id"],
        "route": route,
    }
    with Session(database_engine) as db:
        before = _scope(db, **kwargs)["basis"]
    with Session(database_engine) as db, db.begin():
        connection = db.get(TikTokConnection, route.connection_id)
        if change == "credential":
            connection.credential_revision += 1
        elif change == "observed":
            grant = db.get(
                BCAccountAccess,
                (
                    route.tenant_id,
                    route.bc_id,
                    case["advertiser_id"],
                    route.connection_id,
                ),
            )
            grant.checked_at = datetime.now(UTC)
            # 发现 run 的真实模型变化由完整 SceneJob 连续执行测试覆盖。
            authorization = db.exec(
                select(ConnectionAuthorization).where(
                    ConnectionAuthorization.connection_id == route.connection_id
                )
            ).one()
            authorization.verified_at = datetime.now(UTC)
        else:
            # 删除默认后原route仍须可用；新任务没有默认将明确失败。
            db.delete(db.get(BCDefaultRoute, (route.tenant_id, route.bc_id)))
    with Session(database_engine) as db:
        assert _scope(db, **kwargs)["basis"] == before


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize(
    "change,code",
    [
        ("authorization", "route_authorization_changed"),
        ("contract", "route_contract_changed"),
    ],
)
def test_changed_semantics_reject_original_scene_scope(
    database_engine, scene_case, change, code
):
    case = scene_case
    with Session(database_engine) as db, db.begin():
        connection = db.get(TikTokConnection, case["route"].connection_id)
        if change == "authorization":
            connection.authorization_revision += 1
        else:
            connection.adapter_contract_revision = "changed-contract"
    with Session(database_engine) as db, pytest.raises(DomainError) as error:
        _scope(
            db,
            context=case["context"],
            bc_id=case["route"].bc_id,
            advertiser_id=case["advertiser_id"],
            link_id=case["link_id"],
            route=case["route"],
        )
    assert error.value.code == code


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_new_job_persists_the_exact_parent_route(database_engine, scene_case):
    from app.modules.builds.scene_job_models import SceneJob

    receipt = ensure(database_engine, scene_case)
    assert receipt.state == "queued", receipt.reason_code
    with Session(database_engine) as db:
        assert db.get(SceneJob, receipt.job_id).frozen_route == scene_case[
            "route"
        ].model_dump(mode="json")


def test_legacy_diagnostic_sender_is_removed():
    from app.modules.builds import scene

    assert not hasattr(scene, "refresh_scene_context")
    assert not hasattr(scene, "_scope_capabilities")


def test_scene_page_retains_actual_mcp_evidence_columns():
    from app.modules.builds.scene_job_models import SceneJobPage

    for name in ("mcp_request_id", "remote_task_id"):
        column = SceneJobPage.__table__.columns.get(name)
        assert column is not None
        assert column.nullable and column.type.length == 128


def test_scene_basis_contains_frozen_semantics_and_business_constraints():
    from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
    from app.modules.builds import scene

    route = FrozenTikTokRoute(
        tenant_id=uuid4(),
        bc_id="bc",
        connection_id=uuid4(),
        channel="OFFICIAL_MCP",
        authorization_revision=2,
        adapter_contract_revision="contract",
    )
    business = {
        "advertiser_id": "account",
        "currency": "USD",
        "timezone": "UTC",
        "provider_id": "provider",
        "provider_version": 1,
        "provider_verification": "verified",
        "application_id": "app",
        "minis_id": "minis",
    }
    assert hasattr(scene, "scene_scope_basis"), "稳定摘要必须以冻结语义和业务依据为输入"
    basis = scene.scene_scope_basis(route=route, business=business)
    assert basis == scene.scene_scope_basis(
        route=route, business=dict(reversed(list(business.items())))
    )
    assert basis != scene.scene_scope_basis(
        route=route.model_copy(update={"authorization_revision": 3}), business=business
    )
    assert basis != scene.scene_scope_basis(
        route=route, business={**business, "minis_id": "other"}
    )


def test_draft_account_resolution_uses_its_explicit_connection_without_default(
    database_engine, scene_case
):
    from app.modules.accounts.resolver import resolve_lines
    from app.modules.accounts.schemas import InputLine

    case = scene_case
    route = case["route"]
    with Session(database_engine) as db, db.begin():
        db.delete(db.get(BCDefaultRoute, (route.tenant_id, route.bc_id)))
    with Session(database_engine) as db:
        result = resolve_lines(
            db,
            context=case["context"],
            bc_id=route.bc_id,
            connection_id=route.connection_id,
            lines=[InputLine(line_no=1, raw=case["advertiser_id"])],
        )
        assert result[0].status == "MATCHED"
        assert result[0].advertiser_id == case["advertiser_id"]
