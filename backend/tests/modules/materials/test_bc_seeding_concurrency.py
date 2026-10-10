"""真实 PG 并发提交同内容不同目标，复用唯一 BC 转存任务。"""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from sqlmodel import Session, select

from app.core.db import engine
from app.modules.materials.models import MaterialDistribution, MaterialFile
from app.modules.materials.seed_models import MaterialBCSeed
from tests.modules.materials.test_bc_seeding import seed_env as seed_env
from tests.modules.materials.test_distribution import queue, run, state
from tests.modules.materials.test_readiness import target
from tests.modules.materials.test_remote_only_distribution import info
from tests.modules.materials.test_remote_only_distribution import (
    remote_env as remote_env,
)
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire
from tests.modules.materials.test_url_ingest import url_env as url_env


def test_parallel_aliases_seed_once_then_share_three_targets_and_reuse_for_fourth(
    seed_env, redis_client, wire
):
    envs = [seed_env]
    accounts = [seed_env["target"]]
    with Session(engine) as db, db.begin():
        original = db.get(MaterialFile, seed_env["material_id"])
        for i in (2, 3):
            alias = MaterialFile(
                **(
                    original.model_dump()
                    | {
                        "id": uuid4(),
                        "object_key": str(uuid4()),
                        "file_name": f"alias-{i}.mp4",
                    }
                )
            )
            db.add(alias)
            envs.append({**seed_env, "material_id": alias.id})
            accounts.append(target(db, seed_env, advertiser_id=f"target-b{i}"))
    with ThreadPoolExecutor(max_workers=3) as pool:
        prepared = list(
            pool.map(lambda pair: queue(*pair), zip(envs, accounts, strict=True))
        )
    assert all(row.state == "queued" for row in prepared)
    with Session(engine) as db:
        seeds = db.exec(
            select(MaterialBCSeed).where(
                MaterialBCSeed.tenant_id == seed_env["context"].tenant_id
            )
        ).all()
        assert len(seeds) == 1
        seed_id = seeds[0].distribution_id
        assert all(
            db.get(MaterialDistribution, p.task_id).seed_id == seeds[0].id
            for p in prepared
        )
    wire[1].extend(
        [info(), [{"video_id": "primary-vid", "material_id": "primary-mid"}]]
    )
    run(seed_env, redis_client, seed_id, kind="prepare")
    # 上传成功回执即可信任：种子在 prepare 后直接 ready，不再自动回读。
    assert state(seed_id)[0].status == "ready"
    assert [call[0] for call in wire[0]] == ["GET", "POST"]
    # 重复投递的核实消息不能再访问远端，更不能再次上传。
    run(seed_env, redis_client, seed_id)
    assert state(seed_id)[0].status == "ready"
    assert len(wire[0]) == 2
    for index, result in enumerate(prepared):
        wire[1].extend(
            [
                info(
                    vid="primary-vid",
                    material_id="primary-mid",
                    file_name="primary.mp4",
                ),
                {},
            ]
        )
        run(seed_env, redis_client, result.task_id, kind="prepare")
        # 共享 ACK 不等于目标可用：先按源 VID 查目标账户详情；本场景目标
        # 账户里没有源 VID，再按持久文件名分页发现目标实际 VID。
        wire[1].append({"list": []})
        wire[1].append(
            {
                **info(vid=f"target-{index}", file_name="primary.mp4"),
                "page_info": {
                    "page": 1,
                    "page_size": 100,
                    "total_page": 1,
                    "total_number": 1,
                },
            }
        )
        run(seed_env, redis_client, result.task_id)
        assert state(result.task_id)[0].status == "ready"
        assert state(result.task_id)[2].advertiser_id == accounts[index]
    with Session(engine) as db, db.begin():
        fourth = target(db, seed_env, advertiser_id="target-b4")
    next_target = queue(seed_env, fourth)
    assert state(next_target.task_id)[1].remote_response["transport"] == "native_share"
    uploads = [dict(c[2]["fields"]) for c in wire[0] if "/video/ad/upload/" in c[1]]
    assert [c["advertiser_id"] for c in uploads] == ["primary-b"]
    # 每个预置的远端响应都恰好被消费，避免响应错位掩盖多余或缺失的请求。
    assert wire[1] == []
