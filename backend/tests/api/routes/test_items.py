import pytest
from fastapi.testclient import TestClient


@pytest.mark.parametrize("method", ["GET", "POST", "PATCH", "PUT", "DELETE"])
# 固定 ID：随机 UUID 会让每次收集的用例 ID 不同，并行编排无法按 ID 对应结果。
@pytest.mark.parametrize(
    "path", ["/api/items/", "/api/items/00000000-0000-4000-8000-000000000000"]
)
def test_template_item_api_removed(client: TestClient, method: str, path: str) -> None:
    assert client.request(method, path).status_code == 404
