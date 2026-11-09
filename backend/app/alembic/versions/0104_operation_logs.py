"""Versioned official operation logs and daily compensation evidence."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0104_operation_logs"
down_revision = "0106_report_snapshot_evidence"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "operation_export",
        sa.Column("id", pg.UUID(), primary_key=True),
        sa.Column("tenant_id", pg.UUID(), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("advertiser_id", sa.String(128), nullable=False),
        sa.Column("bc_id", sa.String(128), nullable=False),
        sa.Column("frozen_route", pg.JSONB(), nullable=False),
        sa.Column("task_id", sa.String(128), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timezone", sa.String(128), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("archive_key", sa.String(), nullable=False),
        sa.Column("byte_length", sa.Integer(), nullable=False),
        sa.Column("parser_version", sa.Integer(), nullable=False),
        sa.Column("event_count", sa.Integer(), nullable=False),
    )
    op.create_index(
        "ix_operation_export_account",
        "operation_export",
        ["tenant_id", "advertiser_id", "start_at", "end_at"],
    )
    op.create_table(
        "operation_log_event",
        sa.Column(
            "export_id",
            pg.UUID(),
            sa.ForeignKey("operation_export.id"),
            primary_key=True,
        ),
        sa.Column("identity", sa.String(96), primary_key=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("object_type", sa.String(32), nullable=False),
        sa.Column("object_id", sa.String(128), nullable=False),
        sa.Column("details", pg.JSONB(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("attribution", sa.String(16), nullable=False),
    )
    op.create_table(
        "operation_coverage",
        sa.Column("tenant_id", pg.UUID(), primary_key=True),
        sa.Column("advertiser_id", sa.String(128), primary_key=True),
        sa.Column("stat_date", sa.Date(), primary_key=True),
        sa.Column(
            "export_id", pg.UUID(), sa.ForeignKey("operation_export.id"), nullable=False
        ),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "advertiser_id"],
            ["advertiser_account.tenant_id", "advertiser_account.advertiser_id"],
        ),
    )
    op.create_table(
        "operation_observation",
        sa.Column("id", pg.UUID(), primary_key=True),
        sa.Column("tenant_id", pg.UUID(), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("advertiser_id", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("remote_id", sa.String(128), nullable=False),
        sa.Column("parent_id", sa.String(128)),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observation_day", sa.Date(), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("configuration", pg.JSONB(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "advertiser_id",
            "kind",
            "remote_id",
            "digest",
            "observation_day",
            name="uq_operation_observation",
        ),
    )
    op.create_index(
        "ix_operation_observation_account",
        "operation_observation",
        ["tenant_id", "advertiser_id", "observed_at"],
    )
    op.create_table(
        "operation_day_evidence",
        sa.Column("id", pg.UUID(), primary_key=True),
        sa.Column("tenant_id", pg.UUID(), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("advertiser_id", sa.String(128), nullable=False),
        sa.Column("campaign_id", sa.String(128), nullable=False),
        sa.Column("stat_date", sa.Date(), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("data", pg.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "advertiser_id",
            "campaign_id",
            "stat_date",
            "digest",
            name="uq_operation_day_evidence",
        ),
    )

    op.create_table(
        "operation_sync_cursor",
        sa.Column("tenant_id", pg.UUID(), sa.ForeignKey("tenant.id"), primary_key=True),
        sa.Column("advertiser_id", sa.String(128), primary_key=True),
        sa.Column("recent_day", sa.Date()),
        sa.Column("history_cursor", sa.Date()),
        sa.Column("history_sweep_day", sa.Date()),
    )
    op.add_column("business_evidence", sa.Column("evidence_revision", sa.String(64)))
    # 在实际目录发布事务内保留观察；目录旧值被覆盖前后均有不可变版本。
    op.execute("""
        CREATE FUNCTION retain_operation_observation() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE config jsonb;
        BEGIN
          IF NEW.kind IN ('campaign','adgroup') AND NEW.source_channel='OFFICIAL_API' THEN
            config := NEW.configuration || jsonb_build_object(
                'ad_type',NEW.ad_type,'operation_status',NEW.operation_status);
            INSERT INTO operation_observation
              (id,tenant_id,advertiser_id,kind,remote_id,parent_id,observed_at,
               observation_day,digest,configuration,source)
            VALUES (gen_random_uuid(),NEW.tenant_id,NEW.advertiser_id,NEW.kind,
              NEW.remote_id,NEW.parent_remote_id,NEW.observed_at,
              (NEW.observed_at AT TIME ZONE 'UTC')::date,md5(config::text),config,'OFFICIAL_DIRECTORY')
            ON CONFLICT ON CONSTRAINT uq_operation_observation DO NOTHING;
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER retain_operation_observation AFTER INSERT OR UPDATE ON ad_object
        FOR EACH ROW EXECUTE FUNCTION retain_operation_observation();
        INSERT INTO operation_observation
          (id,tenant_id,advertiser_id,kind,remote_id,parent_id,observed_at,
           observation_day,digest,configuration,source)
        SELECT gen_random_uuid(),tenant_id,advertiser_id,kind,remote_id,parent_remote_id,
          observed_at,(observed_at AT TIME ZONE 'UTC')::date,
          md5((configuration || jsonb_build_object('ad_type',ad_type,'operation_status',operation_status))::text),
          configuration || jsonb_build_object('ad_type',ad_type,'operation_status',operation_status),
          'OFFICIAL_DIRECTORY'
        FROM ad_object WHERE kind IN ('campaign','adgroup') AND source_channel='OFFICIAL_API';
    """)


def downgrade():
    op.execute("DROP TRIGGER retain_operation_observation ON ad_object")
    op.execute("DROP FUNCTION retain_operation_observation()")
    op.drop_column("business_evidence", "evidence_revision")
    op.drop_table("operation_sync_cursor")
    for name in (
        "operation_day_evidence",
        "operation_observation",
        "operation_coverage",
        "operation_log_event",
        "operation_export",
    ):
        op.drop_table(name)
