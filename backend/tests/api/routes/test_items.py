from uuid import uuid4

import pytest
from fastapi.testclient import TestClient


@pytest.mark.parametrize("method", ["GET", "POST", "PATCH", "PUT", "DELETE"])
@pytest.mark.parametrize("path", ["/api/items/", f"/api/items/{uuid4()}"])
def test_template_item_api_removed(client: TestClient, method: str, path: str) -> None:
    assert client.request(method, path).status_code == 404
