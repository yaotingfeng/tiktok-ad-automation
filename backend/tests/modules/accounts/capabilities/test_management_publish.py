"""账户能力发布同时发布广告管理能力：官方授权父级 scope 2 + 实际 ADMIN/OPERATOR 角色。"""

import json

import pytest
from sqlmodel import Session, select

from app.core.db import engine
from app.integrations.tiktok.official.authorization import (
    management_operations_for_scopes,
)
from app.modules.accounts.connection_models import ConnectionAuthorization
from app.modules.accounts.management_capability_models import ManagementCapability
from app.modules.accounts.models import TikTokConnection
from tests.modules.accounts.capabilities.test_service import page, run, start


def test_scope_mapping():
    # 父级 2 覆盖广告管理（系列/广告组/广告的状态、预算、出价）；素材开关还需创意管理 6/61/611。
    assert management_operations_for_scopes({2}) == frozenset(
        {"set_status", "update_budget", "update_roas"}
    )
    assert management_operations_for_scopes({2, 6}) == frozenset(
        {"set_status", "update_budget", "update_roas", "set_material_status"}
    )
    assert management_operations_for_scopes({6, 61}) == frozenset()


def _set_scope(env, scope):
    from app.core.credentials import encrypt_credentials

    values = json.loads(scope)
    with Session(engine) as session, session.begin():
        conn = session.get(TikTokConnection, env["connection_id"])
        conn.credential_ciphertext = encrypt_credentials(
            tenant_id=conn.tenant_id, value={"access_token": "t", "scope": scope}
        )
        authorization = session.exec(
            select(ConnectionAuthorization).where(
                ConnectionAuthorization.connection_id == conn.id
            )
        ).one()
        authorization.scopes = [str(v) for v in values]
        # 与目录完成时写入的摘要同一口径。
        authorization.permission_summary = {
            "read_authorized": True,
            "build_authorized": 2 in values,
            "upload_authorized": bool(set(values) & {6, 61, 611}),
            "management_operations": sorted(
                management_operations_for_scopes(set(values))
            ),
        }


@pytest.mark.parametrize(
    ("scope", "role", "verified"),
    [
        (
            "[2,6]",
            "ADMIN",
            {"set_status", "update_budget", "update_roas", "set_material_status"},
        ),
        ("[2]", "OPERATOR", {"set_status", "update_budget", "update_roas"}),
        ("[2,6]", "ANALYST", set()),
        ("[6]", "ADMIN", set()),
    ],
)
def test_publish_records_management_capabilities(
    capability_env, wire, redis_client, scope, role, verified
):
    env = capability_env
    _set_scope(env, scope)
    job_id = start(env)
    wire[1].append(page(["actual-account"], role=role))
    run(env, redis_client, job_id)
    job = run(env, redis_client, job_id)
    assert job.status == "COMPLETE"
    with Session(engine) as session:
        rows = session.exec(
            select(ManagementCapability).where(
                ManagementCapability.tenant_id == env["context"].tenant_id,
                ManagementCapability.advertiser_id == "actual-account",
            )
        ).all()
        # 每次发布都写出完整矩阵；未证明的显式 UNKNOWN。
        assert len(rows) == 7
        assert {r.operation for r in rows if r.state == "VERIFIED"} == verified
        assert all(r.connection_id == env["connection_id"] for r in rows)


def test_existing_authorization_without_operations_uses_stored_scopes(
    capability_env, wire, redis_client
):
    """上线前保存的授权摘要没有 management_operations：按已保存的官方 scope 推导，无需重新授权。"""
    env = capability_env
    _set_scope(env, "[2,6]")
    with Session(engine) as session, session.begin():
        authorization = session.exec(
            select(ConnectionAuthorization).where(
                ConnectionAuthorization.connection_id == env["connection_id"]
            )
        ).one()
        summary = dict(authorization.permission_summary)
        summary.pop("management_operations")
        authorization.permission_summary = summary
    job_id = start(env)
    wire[1].append(page(["actual-account"], role="ADMIN"))
    run(env, redis_client, job_id)
    run(env, redis_client, job_id)
    with Session(engine) as session:
        verified = {
            r.operation
            for r in session.exec(
                select(ManagementCapability).where(
                    ManagementCapability.advertiser_id == "actual-account",
                    ManagementCapability.state == "VERIFIED",
                )
            ).all()
        }
    assert verified == {
        "set_status",
        "update_budget",
        "update_roas",
        "set_material_status",
    }


def test_non_numeric_stored_scopes_do_not_break_publish(
    capability_env, wire, redis_client
):
    """授权里混有非数字 scope（如 MCP 的 mcp:tt4b）时只忽略它，不能让整页能力发布失败。"""
    env = capability_env
    _set_scope(env, "[2,6]")
    with Session(engine) as session, session.begin():
        authorization = session.exec(
            select(ConnectionAuthorization).where(
                ConnectionAuthorization.connection_id == env["connection_id"]
            )
        ).one()
        summary = dict(authorization.permission_summary)
        summary.pop("management_operations")
        authorization.permission_summary = summary
        authorization.scopes = ["mcp:tt4b", "2", "6"]
    job_id = start(env)
    wire[1].append(page(["actual-account"], role="ADMIN"))
    run(env, redis_client, job_id)
    job = run(env, redis_client, job_id)
    assert job.status == "COMPLETE", job.error_code


def test_binding_generation_mismatch_skips_management_only(
    capability_env, wire, redis_client
):
    """绑定记录的授权代数与连接不一致时管理证据无法挂靠：只跳过管理能力，搭建/上传照常发布。"""
    from app.modules.accounts.connection_models import BCConnectionBinding
    from app.modules.accounts.models import BCAccountAccess

    env = capability_env
    _set_scope(env, "[2,6]")
    with Session(engine) as session, session.begin():
        binding = session.get(
            BCConnectionBinding,
            (env["context"].tenant_id, env["bc_id"], env["connection_id"]),
        )
        binding.authorization_revision += 7
    job_id = start(env)
    wire[1].append(page(["actual-account"], role="ADMIN"))
    run(env, redis_client, job_id)
    job = run(env, redis_client, job_id)
    assert job.status == "COMPLETE", job.error_code
    with Session(engine) as session:
        grant = session.exec(
            select(BCAccountAccess).where(
                BCAccountAccess.advertiser_id == "actual-account"
            )
        ).one()
        assert grant.can_build
        assert (
            session.exec(
                select(ManagementCapability).where(
                    ManagementCapability.advertiser_id == "actual-account"
                )
            ).all()
            == []
        )
