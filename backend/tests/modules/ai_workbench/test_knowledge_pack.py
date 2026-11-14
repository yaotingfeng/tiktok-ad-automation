import json
from pathlib import Path

from app.modules.ai_workbench.knowledge import KnowledgeInput


def test_new_drama_subsidy_is_per_drama_not_new_customer_group():
    path = (
        Path(__file__).resolve().parents[4]
        / "docs/knowledge-packs-new-drama-subsidy-q3-q4.json"
    )
    value = KnowledgeInput.model_validate(json.loads(path.read_text(encoding="utf-8")))
    assert value.scope == "PUBLIC" and value.source_verified is False
    assert "Q4" in value.body and "用户确认" in value.body
    assert "$800" in value.body and "$500" in value.body and "$9,000" in value.body
    assert "首次产生消耗起 30 天" not in value.body
    assert "12,000" not in value.body
    assert "GroupID" in value.body and "不同政策" in value.body
    assert value.expires_at is not None
