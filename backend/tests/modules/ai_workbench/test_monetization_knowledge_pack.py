import json
from pathlib import Path

from app.modules.ai_workbench.knowledge import KnowledgeInput


def test_monetization_pack_separates_modes_and_business_facts():
    path = Path(__file__).resolve().parents[4] / "docs/knowledge-packs-monetization-models.json"
    value = KnowledgeInput.model_validate(json.loads(path.read_text(encoding="utf-8")))
    assert value.scope == "PUBLIC"
    assert value.source_verified is False
    assert {"business", "policy"}.issubset(value.tags)
    for term in ("IAP", "IAA", "端原生", "平台 ROAS", "实际到账"):
        assert term in value.body
    assert "不能" in value.body
    assert value.expires_at is not None
