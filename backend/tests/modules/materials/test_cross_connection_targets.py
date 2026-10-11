"""目标账户素材属于账户本身，搭建连接与历史上传连接不同时仍可复用。

场景：同一 BC 下连接 A 曾把视频传到目标账户，之后 A 失效（停用/重新授权），
默认搭建连接 B 对同一账户权限完好。目标侧读取与封面上传都用 B 发出，
历史上传连接只是留档，不能挡住 B 复用已确认的 VID。
"""

from datetime import UTC, datetime
from uuid import uuid4

from sqlmodel import Session

from app.core.db import engine
from app.modules.accounts.models import BCAccountAccess, TikTokConnection
from app.modules.accounts.routing import freeze_route
from app.modules.materials import covers
from app.modules.materials.cover_models import MaterialCoverJob
from app.modules.materials.models import AccountMaterial, MaterialFile
from app.modules.materials.readiness import get_material_readiness
from tests.modules.materials.test_readiness import runtime_config as runtime_config
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire

TARGET = "actual-account"


def retired_uploader(session: Session, env) -> TikTokConnection:
    """历史上传连接 A：对目标账户的授权已在重新授权时被清空。"""
    connection = TikTokConnection(tenant_id=env["context"].tenant_id, status="DISABLED")
    session.add(connection)
    session.flush()
    session.add(
        BCAccountAccess(
            tenant_id=env["context"].tenant_id,
            bc_id=env["bc_id"],
            advertiser_id=TARGET,
            connection_id=connection.id,
            permission_state="UNKNOWN",
            checked_at=datetime.now(UTC),
        )
    )
    session.flush()
    return connection


def mapped_via(session: Session, env, connection_id, material_id) -> AccountMaterial:
    row = AccountMaterial(
        tenant_id=env["context"].tenant_id,
        bc_id=env["bc_id"],
        material_id=material_id,
        advertiser_id=TARGET,
        connection_id=connection_id,
        video_id="vid-uploaded-by-retired-connection",
        mid="retired-mid",
        status="available",
        verified_at=datetime.now(UTC),
    )
    session.add(row)
    session.flush()
    return row


def test_build_cover_uses_current_route_for_asset_uploaded_by_other_connection(
    source_env, wire
):
    with Session(engine) as session, session.begin():
        retired = retired_uploader(session, source_env)
        mapped_via(session, source_env, retired.id, source_env["material_id"])
    with Session(engine) as session, session.begin():
        result = covers.ensure_cover(
            session,
            context=source_env["context"],
            bc_id=source_env["bc_id"],
            material_id=source_env["material_id"],
            advertiser_id=TARGET,
            task_key=f"build:{uuid4()}",
            route=freeze_route(
                session, context=source_env["context"], bc_id=source_env["bc_id"]
            ),
        )
    assert result.state == "queued"
    with Session(engine) as session:
        job = session.get(MaterialCoverJob, result.task_id)
        # 封面由当前搭建连接上传，复用的是账户上已确认的同一 VID。
        assert job.connection_id == source_env["connection_id"]
        assert job.video_id == "vid-uploaded-by-retired-connection"
    assert not wire[0]


def test_same_content_target_copy_from_other_connection_is_reused(source_env, wire):
    with Session(engine) as session, session.begin():
        retired = retired_uploader(session, source_env)
        original = session.get(MaterialFile, source_env["material_id"])
        original.digest_verified_at = datetime.now(UTC)
        # 同内容再次导入得到另一份内部素材；平台上只需一个 VID。
        duplicate = MaterialFile(
            **{
                **original.model_dump(),
                "id": uuid4(),
                "object_key": f"test/{uuid4()}",
                "file_name": "Moon-again.mp4",
            }
        )
        session.add(duplicate)
        session.flush()
        mapped_via(session, source_env, retired.id, duplicate.id)
    with Session(engine) as session:
        result = get_material_readiness(
            session,
            context=source_env["context"],
            bc_id=source_env["bc_id"],
            material_id=source_env["material_id"],
            advertiser_id=TARGET,
        )
    assert (result.state, result.path) == ("preparable", "existing_target")
    assert result.mapping.video_id == "vid-uploaded-by-retired-connection"
    assert not wire[0]


def test_same_content_copy_still_requires_current_route_permission(source_env, wire):
    """放宽的只是历史上传连接；当前搭建连接没有读权限时不能复用。"""
    with Session(engine) as session, session.begin():
        retired = retired_uploader(session, source_env)
        original = session.get(MaterialFile, source_env["material_id"])
        original.digest_verified_at = datetime.now(UTC)
        duplicate = MaterialFile(
            **{**original.model_dump(), "id": uuid4(), "object_key": f"test/{uuid4()}"}
        )
        session.add(duplicate)
        session.flush()
        mapped_via(session, source_env, retired.id, duplicate.id)
        session.get(
            BCAccountAccess,
            (
                source_env["context"].tenant_id,
                source_env["bc_id"],
                TARGET,
                source_env["connection_id"],
            ),
        ).permission_state = "UNKNOWN"
    with Session(engine) as session:
        result = get_material_readiness(
            session,
            context=source_env["context"],
            bc_id=source_env["bc_id"],
            material_id=source_env["material_id"],
            advertiser_id=TARGET,
        )
    assert result.state == "blocked"
    assert not wire[0]
