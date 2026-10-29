"""Add immutable V2 creative snapshots and explicit historical layout metadata."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0060_frozen_creative_units"
down_revision = "0059_video_creative_units"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "preview_contract",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("preview_id", sa.Uuid(), nullable=False),
        sa.Column("bc_id", sa.String(length=128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("media_kind", sa.String(length=16), nullable=False),
        sa.Column(
            "legacy_material_layout",
            sa.String(length=16),
            nullable=True,
        ),
        sa.CheckConstraint(
            "(version = 1 AND media_kind = 'VIDEO' AND legacy_material_layout IS NOT NULL AND legacy_material_layout IN ('GROUP','AD','UNKNOWN')) OR (version = 2 AND media_kind IN ('VIDEO','CAROUSEL') AND legacy_material_layout IS NULL)",
            name="ck_preview_contract",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "preview_id", "bc_id"],
            ["build_preview.tenant_id", "build_preview.id", "build_preview.bc_id"],
        ),
        sa.PrimaryKeyConstraint("tenant_id", "preview_id"),
    )
    op.create_table(
        "preview_creative_unit",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("preview_id", sa.Uuid(), nullable=False),
        sa.Column("bc_id", sa.String(length=128), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("drama_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("content_revision", sa.Integer(), nullable=False),
        sa.Column("music_revision", sa.Integer(), nullable=False),
        sa.Column(
            "content_digest",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("music_id", sa.String(length=255), nullable=True),
        sa.Column(
            "music_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.CheckConstraint(
            "(kind = 'VIDEO' AND music_id IS NULL) OR (kind = 'CAROUSEL' AND music_id IS NOT NULL AND length(trim(music_id)) > 0)",
            name="ck_frozen_creative_kind",
        ),
        sa.CheckConstraint(
            "position > 0 AND generation > 0 AND content_revision > 0 AND music_revision >= 0",
            name="ck_frozen_creative_versions",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "drama_id"],
            ["provider_drama.tenant_id", "provider_drama.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "preview_id", "bc_id"],
            ["build_preview.tenant_id", "build_preview.id", "build_preview.bc_id"],
        ),
        sa.PrimaryKeyConstraint("tenant_id", "preview_id", "id"),
        sa.UniqueConstraint(
            "tenant_id",
            "preview_id",
            "bc_id",
            "drama_id",
            "id",
            name="uq_frozen_creative_scope",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "preview_id",
            "drama_id",
            "position",
            name="uq_frozen_creative_position",
        ),
    )
    op.create_table(
        "preview_creative_unit_item",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("preview_id", sa.Uuid(), nullable=False),
        sa.Column("bc_id", sa.String(length=128), nullable=False),
        sa.Column("creative_unit_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("drama_id", sa.Uuid(), nullable=False),
        sa.Column("material_id", sa.Uuid(), nullable=False),
        sa.Column("content_digest", sa.String(length=64), nullable=True),
        sa.Column("file_name", sa.String(length=1000), nullable=False),
        sa.CheckConstraint("position BETWEEN 1 AND 35", name="ck_frozen_item_position"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "material_id"],
            ["material_file.tenant_id", "material_file.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "preview_id", "bc_id", "drama_id", "creative_unit_id"],
            [
                "preview_creative_unit.tenant_id",
                "preview_creative_unit.preview_id",
                "preview_creative_unit.bc_id",
                "preview_creative_unit.drama_id",
                "preview_creative_unit.id",
            ],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "preview_id", "bc_id"],
            ["build_preview.tenant_id", "build_preview.id", "build_preview.bc_id"],
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id", "preview_id", "creative_unit_id", "position"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "preview_id",
            "creative_unit_id",
            "material_id",
            name="uq_frozen_item_material",
        ),
    )
    op.create_table(
        "preview_group_creative_unit",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("preview_id", sa.Uuid(), nullable=False),
        sa.Column("bc_id", sa.String(length=128), nullable=False),
        sa.Column("drama_id", sa.Uuid(), nullable=False),
        sa.Column("group_no", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("creative_unit_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("position > 0", name="ck_frozen_group_unit_position"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "preview_id", "bc_id", "drama_id", "creative_unit_id"],
            [
                "preview_creative_unit.tenant_id",
                "preview_creative_unit.preview_id",
                "preview_creative_unit.bc_id",
                "preview_creative_unit.drama_id",
                "preview_creative_unit.id",
            ],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "preview_id", "bc_id"],
            ["build_preview.tenant_id", "build_preview.id", "build_preview.bc_id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "preview_id", "drama_id", "group_no"],
            [
                "preview_drama_group.tenant_id",
                "preview_drama_group.preview_id",
                "preview_drama_group.drama_id",
                "preview_drama_group.group_no",
            ],
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id", "preview_id", "drama_id", "group_no", "position"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "preview_id",
            "drama_id",
            "group_no",
            "creative_unit_id",
            name="uq_frozen_group_unit",
        ),
    )
    op.create_table(
        "preview_ad_creative_unit",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("preview_id", sa.Uuid(), nullable=False),
        sa.Column("bc_id", sa.String(length=128), nullable=False),
        sa.Column("drama_id", sa.Uuid(), nullable=False),
        sa.Column("group_no", sa.Integer(), nullable=False),
        sa.Column("base_ad_no", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("creative_unit_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "base_ad_no > 0 AND position BETWEEN 1 AND 50",
            name="ck_frozen_ad_unit_position",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "preview_id", "bc_id", "drama_id", "creative_unit_id"],
            [
                "preview_creative_unit.tenant_id",
                "preview_creative_unit.preview_id",
                "preview_creative_unit.bc_id",
                "preview_creative_unit.drama_id",
                "preview_creative_unit.id",
            ],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "preview_id", "bc_id"],
            ["build_preview.tenant_id", "build_preview.id", "build_preview.bc_id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "preview_id", "drama_id", "group_no"],
            [
                "preview_drama_group.tenant_id",
                "preview_drama_group.preview_id",
                "preview_drama_group.drama_id",
                "preview_drama_group.group_no",
            ],
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id", "preview_id", "drama_id", "group_no", "base_ad_no", "position"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "preview_id",
            "drama_id",
            "group_no",
            "base_ad_no",
            "creative_unit_id",
            name="uq_frozen_ad_unit",
        ),
    )
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "preview_image_exclusion",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("preview_id", sa.Uuid(), nullable=False),
        sa.Column("bc_id", sa.String(length=128), nullable=False),
        sa.Column("drama_id", sa.Uuid(), nullable=False),
        sa.Column("material_id", sa.Uuid(), nullable=False),
        sa.Column("file_name", sa.String(length=1000), nullable=False),
        sa.Column("reason", sa.String(length=16), nullable=False),
        sa.CheckConstraint(
            "reason IN ('DISABLED','REMAINDER')",
            name="ck_preview_image_exclusion_reason",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "drama_id"],
            ["provider_drama.tenant_id", "provider_drama.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "material_id"],
            ["material_file.tenant_id", "material_file.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "preview_id", "bc_id"],
            ["build_preview.tenant_id", "build_preview.id", "build_preview.bc_id"],
        ),
        sa.PrimaryKeyConstraint("tenant_id", "preview_id", "drama_id", "material_id"),
    )
    # ### end Alembic commands ###
    # 仅在迁移时识别历史布局；运行时绝不靠缺行猜测正文格式。
    op.execute(
        sa.text("""
        INSERT INTO preview_contract (tenant_id, preview_id, bc_id, version, media_kind, legacy_material_layout)
        SELECT p.tenant_id, p.id, p.bc_id, 1, 'VIDEO',
          CASE WHEN EXISTS (SELECT 1 FROM preview_ad_material a WHERE a.tenant_id=p.tenant_id AND a.preview_id=p.id) THEN 'AD'
               WHEN EXISTS (SELECT 1 FROM preview_group_material g WHERE g.tenant_id=p.tenant_id AND g.preview_id=p.id) THEN 'GROUP'
               ELSE 'UNKNOWN' END
        FROM build_preview p
    """)
    )
    for table in (
        "preview_image_exclusion",
        "preview_contract",
        "preview_creative_unit",
        "preview_creative_unit_item",
        "preview_group_creative_unit",
        "preview_ad_creative_unit",
    ):
        op.execute(
            f"CREATE TRIGGER {table}_frozen BEFORE INSERT OR UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION check_preview_child_write()"
        )
    # 现行 V2 只有单元/成员表；这些只读投影供旧视频执行/目录查询使用。
    for level in ("group", "ad"):
        base = ",m.base_ad_no" if level == "ad" else ""
        partition = ",m.base_ad_no" if level == "ad" else ""
        op.execute(f"""
          CREATE VIEW frozen_{level}_material AS
          SELECT m.tenant_id,m.preview_id,m.bc_id,m.drama_id,m.group_no{base},m.position,m.material_id
          FROM preview_{level}_material m JOIN preview_contract c
            ON c.tenant_id=m.tenant_id AND c.preview_id=m.preview_id AND c.bc_id=m.bc_id AND c.version=1
          UNION ALL
          SELECT m.tenant_id,m.preview_id,m.bc_id,m.drama_id,m.group_no{base},
            row_number() OVER (PARTITION BY m.tenant_id,m.preview_id,m.bc_id,m.drama_id,m.group_no{partition} ORDER BY m.position,i.position),i.material_id
          FROM preview_{level}_creative_unit m
          JOIN preview_creative_unit_item i ON i.tenant_id=m.tenant_id AND i.preview_id=m.preview_id AND i.bc_id=m.bc_id AND i.drama_id=m.drama_id AND i.creative_unit_id=m.creative_unit_id
          JOIN preview_contract c ON c.tenant_id=m.tenant_id AND c.preview_id=m.preview_id AND c.bc_id=m.bc_id AND c.version=2
        """)
    # 仅替换素材归属证据源；保留完整的租户/账户/父级/不可变请求保护。
    definition = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT pg_get_functiondef('build_execution_scope()'::regprocedure)"
            )
        )
        .scalar_one()
    )
    old = "FROM preview_group_material WHERE"
    if definition.count(old) != 1:
        raise RuntimeError("unexpected execution scope definition")
    op.execute(definition.replace(old, "FROM frozen_group_material WHERE"))

    # 最终冻结时验证每个完整单元及其有序成员，不允许直接 SQL 绕过应用校验。
    op.execute("""
      CREATE FUNCTION check_preview_v2_complete() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF NEW.status='FROZEN' AND OLD.status='BUILDING' AND EXISTS (
          SELECT 1 FROM preview_contract c WHERE c.tenant_id=NEW.tenant_id AND c.preview_id=NEW.id AND c.version=2
        ) THEN
          IF EXISTS (
            SELECT 1 FROM preview_creative_unit u JOIN preview_contract c
              ON c.tenant_id=u.tenant_id AND c.preview_id=u.preview_id
            WHERE u.tenant_id=NEW.tenant_id AND u.preview_id=NEW.id AND u.kind<>c.media_kind
          ) OR EXISTS (
            SELECT 1 FROM preview_creative_unit_item i
            JOIN preview_creative_unit u ON u.tenant_id=i.tenant_id AND u.preview_id=i.preview_id AND u.id=i.creative_unit_id
            JOIN material_file f ON f.tenant_id=i.tenant_id AND f.id=i.material_id
            WHERE i.tenant_id=NEW.tenant_id AND i.preview_id=NEW.id AND
              ((u.kind='VIDEO' AND f.media_type<>'VIDEO') OR (u.kind='CAROUSEL' AND (f.media_type<>'IMAGE' OR i.content_digest IS NULL)))
          ) THEN RAISE EXCEPTION 'frozen creative format mismatch'; END IF;
          IF EXISTS (
            SELECT 1 FROM planned_ad a JOIN planned_group g
              ON g.tenant_id=a.tenant_id AND g.preview_id=a.preview_id AND g.id=a.group_id
            WHERE a.tenant_id=NEW.tenant_id AND a.preview_id=NEW.id AND NOT EXISTS (
              SELECT 1 FROM preview_ad_creative_unit m WHERE m.tenant_id=a.tenant_id AND m.preview_id=a.preview_id
                AND m.drama_id=g.drama_id AND m.group_no=g.group_no AND m.base_ad_no=a.base_ad_no
            )
          ) THEN RAISE EXCEPTION 'empty frozen ad mapping'; END IF;
          IF EXISTS (
            SELECT 1 FROM preview_creative_unit u LEFT JOIN preview_creative_unit_item i
              ON i.tenant_id=u.tenant_id AND i.preview_id=u.preview_id AND i.creative_unit_id=u.id
            WHERE u.tenant_id=NEW.tenant_id AND u.preview_id=NEW.id
            GROUP BY u.id,u.kind HAVING count(i.position) NOT BETWEEN 1 AND 35
              OR (u.kind='VIDEO' AND count(i.position)<>1)
              OR min(i.position)<>1 OR max(i.position)<>count(i.position)
          ) THEN RAISE EXCEPTION 'incomplete frozen creative unit'; END IF;
        END IF;
        RETURN NEW;
      END $$;
      CREATE TRIGGER preview_v2_complete BEFORE UPDATE ON build_preview
        FOR EACH ROW EXECUTE FUNCTION check_preview_v2_complete();
    """)


def downgrade():
    raise RuntimeError("Frozen V2 intent cannot be rewritten into legacy material rows")
