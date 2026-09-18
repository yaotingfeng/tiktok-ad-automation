"""允许平台明确拒绝的封面共享保留旧代并建立新代。

Revision ID: cover_rejected_reissue
Revises: material_approved_reissue
"""

from alembic import op

revision = "cover_rejected_reissue"
down_revision = "material_approved_reissue"
branch_labels = None
depends_on = None


def _guard(*, allow_rejected_cover: bool) -> str:
    rejected_cover = (
        "OR (NEW.status = 'BLOCKED' AND "
        "NEW.error_code = 'cover_share_rejected')"
        if allow_rejected_cover
        else ""
    )
    return f"""
        CREATE OR REPLACE FUNCTION material_replacement_pointer_guard()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'UPDATE' AND OLD.superseded_by_id IS NOT NULL
               AND NEW.superseded_by_id IS DISTINCT FROM OLD.superseded_by_id THEN
                RAISE EXCEPTION 'immutable material replacement pointer' USING ERRCODE='23514';
            END IF;
            IF NEW.superseded_by_id IS NOT NULL AND
               (TG_OP = 'INSERT' OR OLD.superseded_by_id IS NULL) THEN
                -- 分支后再引用 cover 专有 error_code；共享给三张表的 trigger
                -- 不能在一个布尔表达式里解析不存在于 VIDEO 表的字段。
                IF TG_TABLE_NAME = 'material_cover_job' THEN
                    IF NOT (NEW.status = 'UNKNOWN' {rejected_cover}) THEN
                        RAISE EXCEPTION 'material replacement requires unknown or rejected evidence' USING ERRCODE='23514';
                    END IF;
                ELSIF NEW.status != 'result_unknown' THEN
                    RAISE EXCEPTION 'material replacement requires unknown or rejected evidence' USING ERRCODE='23514';
                END IF;
                IF TG_OP = 'UPDATE' AND OLD.status IS DISTINCT FROM NEW.status THEN
                    RAISE EXCEPTION 'material replacement requires unknown or rejected evidence' USING ERRCODE='23514';
                END IF;
            END IF;
            RETURN NEW;
        END $$;
    """


def upgrade() -> None:
    # 只有平台失败清单中的明确拒绝才由业务层进入此分支；数据库仍禁止
    # 普通 BLOCKED、READY 或状态同时改写的历史任务建立替代指针。
    op.execute(_guard(allow_rejected_cover=True))


def downgrade() -> None:
    op.execute(_guard(allow_rejected_cover=False))
