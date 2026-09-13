"""Separate confirmed promotion targets from provider applications.

Revision ID: automatic_mini_targets
Revises: material_source_bc
"""

import sqlalchemy as sa
from alembic import op

revision = "automatic_mini_targets"
down_revision = "material_source_bc"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "build_mini_target",
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenant.id"), primary_key=True),
        sa.Column("url_digest", sa.String(64), primary_key=True),
        sa.Column("minis_id", sa.String(128), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("user.id"), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source IN ('USER','LINK','LEGACY')", name="ck_build_mini_target_source"
        ),
    )
    # 仅迁移已有确认对应的历史链接；同 URL 对应关系冲突时不猜测。
    op.execute("""INSERT INTO build_mini_target (tenant_id,url_digest,minis_id,source,confirmed_at)
        SELECT l.tenant_id,encode(sha256(convert_to(l.url,'UTF8')),'hex'),min(a.tiktok_minis_id),'LEGACY',CURRENT_TIMESTAMP
        FROM promotion_link l JOIN provider_application a ON a.tenant_id=l.tenant_id AND a.connection_id=l.connection_id AND a.external_id=l.application_id
        WHERE a.tiktok_minis_id IS NOT NULL AND a.tiktok_minis_id<>'' AND l.url IS NOT NULL AND l.url<>'' AND l.status='ready' AND l.verified_at IS NOT NULL
        GROUP BY l.tenant_id,encode(sha256(convert_to(l.url,'UTF8')),'hex') HAVING count(DISTINCT a.tiktok_minis_id)=1""")
    for column in ("provider_connection_id", "application_id", "minis_id"):
        op.alter_column("build_scene_job", column, nullable=True)
    op.drop_constraint(
        "ck_draft_mutation_result", "draft_mutation_request", type_="check"
    )
    op.create_check_constraint(
        "ck_draft_mutation_result",
        "draft_mutation_request",
        "kind IN ('update','groups','minis') AND applied_revision > 0",
    )


def downgrade():
    # 新选择/账户目录任务已存在时不能无损回旧约束，先拒绝而非删除记录。
    count = (
        op.get_bind()
        .exec_driver_sql(
            "SELECT (SELECT count(*) FROM build_mini_target WHERE source<>'LEGACY') + (SELECT count(*) FROM draft_mutation_request WHERE kind='minis') + (SELECT count(*) FROM build_scene_job WHERE provider_connection_id IS NULL OR minis_id IS NULL)"
        )
        .scalar_one()
    )
    if count:
        raise RuntimeError(
            "New Mini selections exist; downgrade would discard confirmed targets"
        )
    for column in ("provider_connection_id", "application_id", "minis_id"):
        op.alter_column("build_scene_job", column, nullable=False)
    op.drop_constraint(
        "ck_draft_mutation_result", "draft_mutation_request", type_="check"
    )
    op.create_check_constraint(
        "ck_draft_mutation_result",
        "draft_mutation_request",
        "kind IN ('update','groups') AND applied_revision > 0",
    )
    op.drop_table("build_mini_target")
