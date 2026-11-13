import json
from pathlib import Path

from app.modules.ai_workbench.knowledge import KnowledgeInput


def test_new_drama_q3_q4_pack_is_scoped_and_preserves_uncertainty():
    path = Path(__file__).resolve().parents[4] / "docs/knowledge-packs-new-drama-subsidy-q3-q4.json"
    body = json.loads(path.read_text(encoding="utf-8"))
    value = KnowledgeInput.model_validate(body)
    assert value.scope == "PUBLIC"
    assert value.source_verified is False
    assert "Q4" in value.body and "用户确认" in value.body
    assert "2,000 < R <= 4,000" in value.body
    assert "实际到账" in value.body
    assert value.expires_at is not None
