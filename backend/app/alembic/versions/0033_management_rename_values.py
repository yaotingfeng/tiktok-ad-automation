"""广告管理支持推广系列改名：预览/任务项的原值与新值列放宽到名称上限 512。"""

import sqlalchemy as sa
from alembic import op

revision = "0033_management_rename_values"
down_revision = "0032_drop_management_capability"
branch_labels = None
depends_on = None

_TABLES = ("management_preview_item", "management_task_item")


def upgrade() -> None:
    for table in _TABLES:
        for column in ("original_value", "final_value"):
            op.alter_column(
                table, column, type_=sa.String(512), existing_type=sa.String(128)
            )


def downgrade() -> None:
    # 回退前须确认没有超过 128 字符的改名值，否则缩列会直接失败而不是截断。
    for table in _TABLES:
        for column in ("original_value", "final_value"):
            op.alter_column(
                table, column, type_=sa.String(128), existing_type=sa.String(512)
            )
