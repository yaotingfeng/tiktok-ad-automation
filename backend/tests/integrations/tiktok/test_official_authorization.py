"""官方 OAuth scope 到授权事实的唯一映射处。"""

from app.integrations.tiktok.official.authorization import (
    management_operations_for_scopes,
)


def test_ads_management_scope_grants_rename():
    assert "rename" in management_operations_for_scopes({2})
    assert "rename" not in management_operations_for_scopes({6})
