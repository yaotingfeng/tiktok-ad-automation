"""允许显式授权的封面领取冲突保留旧代并建立目标直传新代。

Revision ID: cover_claim_reissue
Revises: cover_rejected_reissue
"""

from alembic import op

revision = "cover_claim_reissue"
down_revision = "cover_rejected_reissue"
branch_labels = None
depends_on = None


def _guard(*, allow_claim_lost: bool) -> str:
    blocked_errors = (
        "('cover_share_rejected', 'cover_claim_lost')"
        if allow_claim_lost
        else "('cover_share_rejected')"
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
                IF TG_TABLE_NAME = 'material_cover_job' THEN
                    IF NOT (NEW.status = 'UNKNOWN' OR
                            (NEW.status = 'BLOCKED' AND NEW.error_code IN {blocked_errors})) THEN
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
    # 业务层仍要求显式的 accepted_duplicate_materials 授权、原共享成员、
    # 无图片正回执且无活动 claim；这里只允许旧 BLOCKED 事实建立不可变指针。
    op.execute(_guard(allow_claim_lost=True))


def downgrade() -> None:
    op.execute(_guard(allow_claim_lost=False))
