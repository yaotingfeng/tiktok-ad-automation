"""签名生产谱系使用真实临时库，保留不可变订单/策略历史直到库级清理。"""

import pytest
from sqlmodel import Session

from app.core.db import engine
from app.modules.materials.push_models import PushedMaterial
from app.modules.tenants.models import Tenant
from tests.modules.materials.test_push import URL, items, payload, post
from tests.modules.materials.test_push import client as client
from tests.modules.materials.test_push import push_env as push_env
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.strategies.test_concurrency import (
    isolated_strategy_database as isolated_strategy_database,
)


@pytest.fixture(autouse=True)
def retain_build_history(isolated_strategy_database, monkeypatch):
    isolated = isolated_strategy_database[0]
    for module in ("test_production_push", "test_push", "test_source_uploads"):
        monkeypatch.setattr(f"tests.modules.materials.{module}.engine", isolated)
    return isolated


def test_signed_producer_origin_is_persisted_and_bad_job_is_atomic(client, push_env):
    from tests.modules.ai_workbench.test_production_origin import seed_order

    context = push_env["context"]
    with Session(engine) as db, db.begin():
        # 来源 BC 继续使用既有测试路由；生产谱系是租户事实，不替换上传目标。
        db.get(Tenant, context.tenant_id).default_bc_id = push_env["bc_id"]
        _, order = seed_order(db, context)
        origin = {
            "order_id": str(order.id),
            "job_id": order.job_id,
            "strategy_id": "creative-one",
        }
    body = payload(push_env, ids=("lineage-file",))
    body["materials"][0]["production_origin"] = origin
    response = post(client, push_env, body)
    assert response.status_code == 202, response.text
    saved = items(response.json()["batch_id"])[0]
    assert saved.production_origin == {**origin, "source": "authenticated_producer"}
    bad = payload(push_env, ids=("wrong-job-file",))
    bad["materials"][0]["production_origin"] = {**origin, "job_id": "other-job"}
    rejected = post(client, push_env, bad)
    assert rejected.status_code >= 400
    assert URL not in rejected.text
    with Session(engine) as db:
        assert db.get(PushedMaterial, (context.tenant_id, "wrong-job-file")) is None
