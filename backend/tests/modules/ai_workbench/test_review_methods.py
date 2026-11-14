from hashlib import sha256
from pathlib import Path

import pytest
from pydantic import ValidationError


def plugin_methods():
    """测试消费者读取插件发布资源；生产后端不读取插件目录。"""
    root = (
        Path(__file__).resolve().parents[4]
        / "integrations/dsh-ai-workbench/skills/ai-delivery-review"
    )

    def resource(key, path):
        body = path.read_text()
        return {
            "key": key,
            "description": "test plugin method",
            "version": "1.0.0",
            "sha256": sha256(body.encode()).hexdigest(),
            "body": body,
        }

    return {
        "main": resource("ai-delivery-review", root / "SKILL.md"),
        "references": [
            resource(key, root / "references" / f"{key}.md")
            for key in ("builds", "rules", "materials", "business", "scale")
        ],
    }


def test_method_archive_validates_hashes_and_catalog():
    from app.modules.ai_workbench.methods import MethodBundle

    valid = MethodBundle.model_validate(plugin_methods())
    assert len(valid.references) == 5
    invalid = valid.model_dump()
    invalid["main"]["body"] += "tampered"
    with pytest.raises(ValidationError):
        MethodBundle.model_validate(invalid)
