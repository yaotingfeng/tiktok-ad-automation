from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.modules.ai_workbench.models import AIOptimizerStrategyBinding
from app.modules.ai_workbench.schemas import OptimizerCreate


def payload(**overrides):
    value = {
        "name": "起量优化师",
        "daily_spend": "100",
        "target_roas": "1.2",
        "priority": "ROAS",
        "bc_id": "bc-a",
        "advertiser_ids": ["acc-1"],
        "optimization_mode": "MANUAL",
        "strategy_version_id": str(uuid4()),
    }
    value.update(overrides)
    return value


def test_optimizer_contract_accepts_scoped_configuration():
    result = OptimizerCreate.model_validate(payload())
    assert result.bc_id == "bc-a"
    assert result.optimization_mode == "MANUAL"


def test_optimizer_contract_rejects_invalid_mode_and_roas_priority():
    with pytest.raises(ValidationError):
        OptimizerCreate.model_validate(payload(optimization_mode="INVALID"))
    with pytest.raises(ValidationError):
        OptimizerCreate.model_validate(payload(target_roas=None))


def test_strategy_binding_has_active_scope_index():
    indexes = {index.name for index in AIOptimizerStrategyBinding.__table__.indexes}
    assert "uq_ai_binding_one_active" in indexes
