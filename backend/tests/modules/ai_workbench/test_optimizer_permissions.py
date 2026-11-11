from app.modules.tenants.permissions import PERMISSIONS


def test_viewer_is_read_only_for_optimizer_configuration():
    assert PERMISSIONS["viewer"] == frozenset({"read"})
    assert "strategy_write" not in PERMISSIONS["viewer"]


def test_operator_can_manage_strategy_scoped_optimizer_configuration():
    assert "strategy_write" in PERMISSIONS["operator"]
