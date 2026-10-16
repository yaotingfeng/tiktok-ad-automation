"""目录发布用独立序列分配版本，避免持锁期间扫描全部对象及素材。"""

from alembic import op

revision = "0034_directory_publish_version"
down_revision = "0033_management_rename_values"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 发布流程已冻结全部消费者；包含空目录的运行版本，不能复用历史版本号。
    op.execute("CREATE SEQUENCE ad_directory_published_version_seq AS bigint MINVALUE 1")
    op.execute("""
        SELECT setval('ad_directory_published_version_seq', GREATEST(
            COALESCE((SELECT MAX(published_version) FROM ad_object), 0),
            COALESCE((SELECT MAX(published_version) FROM ad_material_reference), 0),
            COALESCE((SELECT MAX(published_version) FROM ad_directory_run), 0)
        ) + 1, false)
    """)


def downgrade() -> None:
    op.execute("DROP SEQUENCE ad_directory_published_version_seq")
