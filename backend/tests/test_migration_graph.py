"""迁移图必须收敛到单一 head，合并迁移不能偷偷执行 DDL/DML。"""

import ast
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def test_preview_provider_heads_are_merged_without_operations():
    backend = Path(__file__).resolve().parents[1]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "app/alembic"))
    script = ScriptDirectory.from_config(config)

    # 后续迁移会继续前移 head；这里只约束迁移图收敛为单一 head，且已合并分支仍在主线上。
    heads = script.get_heads()
    assert len(heads) == 1
    assert "20261005_merge_preview_heads" in {
        revision.revision for revision in script.walk_revisions("base", heads[0])
    }
    merge = script.get_revision("20261005_merge_preview_heads")
    assert merge.down_revision == (
        "20261005_preview_ad_material",
        "provider_kinds_expansion",
    )

    source = (
        backend / "app/alembic/versions/20261005_merge_preview_provider_heads.py"
    ).read_text()
    tree = ast.parse(source)
    for function_name in ("upgrade", "downgrade"):
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == function_name
        )
        assert all(
            not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "op"
            )
            for node in ast.walk(function)
        )
