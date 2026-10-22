"""广告报表素材详情的成片预览：按账户 + 平台视频 ID 读取，不依赖素材库记录。"""

from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.errors import DomainError, domain_error_handler
from app.core.security import create_access_token
from app.modules.materials.router import router
from tests.modules.materials.test_remote_only_distribution import (
    PREVIEW,
    info,
)
from tests.modules.materials.test_remote_only_distribution import (
    remote_env as remote_env,
)
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire
from tests.modules.materials.test_url_ingest import url_env as url_env
from tests.modules.strategies.test_concurrency import (
    isolated_strategy_database as isolated_strategy_database,
)


@pytest.fixture
def preview(remote_env):
    app = FastAPI()
    app.add_exception_handler(DomainError, domain_error_handler)
    app.include_router(router, prefix="/api")
    client = TestClient(app)
    client.headers["Authorization"] = "Bearer " + create_access_token(
        remote_env["context"].actor_id, timedelta(minutes=5)
    )

    def read(advertiser_ids: str, video_id: str = "vid-actual-account"):
        return client.get(
            f"/api/tenants/{remote_env['context'].tenant_id}/materials/account-video-preview",
            params={
                "bc_id": remote_env["source_bc_id"],
                "video_id": video_id,
                "advertiser_ids": advertiser_ids,
            },
        )

    with client:
        yield read


def test_reads_first_authorized_candidate_without_library_record(preview, wire):
    wire[1].append(info())
    # 第一个候选账户没有授权，跳过它，只对有授权的账户读取一次平台。
    response = preview("no-grant-account,actual-account")
    assert response.status_code == 200, response.json()
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["url"] == PREVIEW
    assert (body["advertiser_id"], body["video_id"]) == (
        "actual-account",
        "vid-actual-account",
    )
    assert [call[0] for call in wire[0]] == ["GET"]


def test_candidates_without_grant_are_rejected_before_network(preview, wire):
    response = preview("no-grant-account,another-stranger")
    assert response.status_code == 409
    assert response.json()["code"] == "material_preview_account_unavailable"
    assert wire[0] == []


def test_returned_video_must_match_request(preview, wire):
    wire[1].append(info(vid="someone-else"))
    response = preview("actual-account")
    # 平台返回的不是请求的视频：结构校验拒绝，绝不把别的视频地址返回给页面。
    assert response.status_code >= 400
    assert PREVIEW not in response.text


def test_candidate_count_is_bounded(preview, wire):
    response = preview(",".join(f"acc-{index}" for index in range(21)))
    assert response.status_code == 422
    assert wire[0] == []
