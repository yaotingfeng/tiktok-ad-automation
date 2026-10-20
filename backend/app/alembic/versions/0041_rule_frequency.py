"""规则及历史运行快照统一使用 15 分钟最小频率。"""

from alembic import op

revision = "0041_rule_frequency"
down_revision = "0040_material_production"
branch_labels = None
depends_on = None


def _replace_frequency(old: str, new: str) -> None:
    # 两列都由 RuleDefinition 直接解析；历史确认/执行也会读取快照，不能只迁规则表。
    for table in ("automation_rule", "automation_rule_run"):
        op.execute(
            f"UPDATE {table} SET definition = jsonb_set(definition, "
            f"'{{schedule,frequency}}', '\"{new}\"'::jsonb, false) "
            f"WHERE definition #>> '{{schedule,frequency}}' = '{old}'"
        )


def upgrade() -> None:
    _replace_frequency("5M", "15M")


def downgrade() -> None:
    _replace_frequency("15M", "5M")
