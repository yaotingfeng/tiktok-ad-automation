"""Independent shadow projection and statement-level three-source invalidation."""

from alembic import op

revision = "0113_creative_material"
down_revision = "0112_creative_report_day"
branch_labels = None
depends_on = None

METRICS = (
    "spend",
    "impressions",
    "clicks",
    "ad_impression_events",
    "revenue_d0",
    "revenue_d3",
    "revenue_d7",
    "revenue_d14",
    "revenue_d30",
    "revenue_total",
)


def upgrade() -> None:
    values = ",".join(
        f"{c} numeric,{c}_has_null boolean NOT NULL DEFAULT false" for c in METRICS
    )
    op.execute(f"""CREATE TABLE creative_material_entity_day (
        tenant_id uuid NOT NULL,advertiser_id varchar(128) NOT NULL,stat_date date NOT NULL,
        currency varchar(16) NOT NULL,timezone varchar(64) NOT NULL,fetched_at timestamptz NOT NULL,source_revision bigint NOT NULL,
        fact_key varchar(512) NOT NULL,row_key varchar(512) NOT NULL,creative_id varchar(128),
        ad_remote_id varchar(128),ad_material_id varchar(128),campaign_id varchar(128) NOT NULL,adgroup_id varchar(128) NOT NULL,
        material_type varchar(64) NOT NULL,platform_material_id varchar(128),material_name varchar,
        assets jsonb NOT NULL,attribution_status varchar(32) NOT NULL,source_status varchar(16) NOT NULL,source_info jsonb NOT NULL,
        {values},PRIMARY KEY(tenant_id,advertiser_id,stat_date,currency,timezone,fact_key),
        FOREIGN KEY(tenant_id,advertiser_id) REFERENCES advertiser_account(tenant_id,advertiser_id) ON DELETE CASCADE)
    """)
    op.execute(
        "CREATE INDEX ix_creative_material_period ON creative_material_entity_day (tenant_id,stat_date,advertiser_id)"
    )
    op.execute(
        "CREATE INDEX ix_creative_material_page ON creative_material_entity_day (tenant_id,row_key,advertiser_id,stat_date)"
    )
    op.execute(
        "CREATE INDEX ix_creative_material_group ON creative_material_entity_day (tenant_id,advertiser_id,adgroup_id,stat_date)"
    )
    op.execute(
        "CREATE INDEX ix_creative_material_ad ON creative_material_entity_day (tenant_id,advertiser_id,ad_remote_id,stat_date)"
    )
    op.execute(
        "CREATE INDEX ix_creative_material_dirty ON read_bucket_state (observed_at,tenant_id,advertiser_id,bucket) WHERE source_revision>derived_revision AND kind='creative_material'"
    )
    for table in (
        "ad_report_day",
        "creative_report_day",
        "ad_creative",
        "report_sync_state",
    ):
        for event in ("INSERT", "UPDATE", "DELETE"):
            rels = (
                ("new_rows",)
                if event == "INSERT"
                else ("old_rows",)
                if event == "DELETE"
                else ("old_rows", "new_rows")
            )
            if table == "ad_creative":
                # 先按完整身份键比较投影消费字段，再查历史事实。观察水位前进不能
                # 让未变化身份反复重建全历史；INSERT/DELETE 仍覆盖全部受影响日期。
                changed = (
                    "ad_remote_id",
                    "ad_material_id",
                    "material_type",
                    "platform_material_id",
                    "name",
                    "assets",
                    "identity_status",
                )
                if event == "UPDATE":
                    old = ",".join(f"o.{c}" for c in changed)
                    new = ",".join(f"n.{c}" for c in changed)
                    identities = f"""SELECT COALESCE(o.tenant_id,n.tenant_id) AS tenant_id,
                        COALESCE(o.advertiser_id,n.advertiser_id) AS advertiser_id,
                        COALESCE(o.creative_id,n.creative_id) AS creative_id
                        FROM old_rows o FULL JOIN new_rows n USING(tenant_id,advertiser_id,creative_id)
                        WHERE o.creative_id IS NULL OR n.creative_id IS NULL
                          OR ROW({old}) IS DISTINCT FROM ROW({new})"""
                else:
                    identities = (
                        f"SELECT tenant_id,advertiser_id,creative_id FROM {rels[0]}"
                    )
                keys = f"""WITH changed_identities AS MATERIALIZED ({identities})
                    SELECT f.tenant_id,f.advertiser_id,f.stat_date
                    FROM changed_identities c JOIN creative_report_day f
                    ON f.tenant_id=c.tenant_id AND f.advertiser_id=c.advertiser_id AND f.creative_id=c.creative_id"""
            else:
                extra = (
                    " WHERE report_kind IN ('ad_day','creative_day')"
                    if table == "report_sync_state"
                    else ""
                )
                keys = " UNION ".join(
                    f"SELECT tenant_id,advertiser_id,stat_date FROM {r}{extra}"
                    for r in rels
                )
            name = f"crm_{table}_{event.lower()}"
            op.execute(f"""CREATE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
                IF NOT EXISTS({" UNION ALL ".join(f"SELECT 1 FROM {r}" for r in rels)}) THEN RETURN NULL; END IF;
                INSERT INTO read_bucket_state(tenant_id,advertiser_id,kind,bucket,source_revision,derived_revision,observed_at)
                SELECT k.tenant_id,k.advertiser_id,'creative_material',k.stat_date,1,0,clock_timestamp()
                FROM (SELECT DISTINCT tenant_id,advertiser_id,stat_date FROM ({keys}) raw_keys) k JOIN advertiser_account a ON a.tenant_id=k.tenant_id AND a.advertiser_id=k.advertiser_id
                ORDER BY k.tenant_id,k.advertiser_id,k.stat_date
                ON CONFLICT(tenant_id,advertiser_id,kind,bucket) DO UPDATE SET
                  source_revision=read_bucket_state.source_revision+1,observed_at=clock_timestamp();
                RETURN NULL; END $$""")
            refs = " ".join(
                (
                    "NEW TABLE AS new_rows"
                    if r == "new_rows"
                    else "OLD TABLE AS old_rows"
                )
                for r in rels
            )
            op.execute(
                f"CREATE TRIGGER {name} AFTER {event} ON {table} REFERENCING {refs} FOR EACH STATEMENT EXECUTE FUNCTION {name}()"
            )


def downgrade() -> None:
    for table in (
        "ad_report_day",
        "creative_report_day",
        "ad_creative",
        "report_sync_state",
    ):
        for event in ("insert", "update", "delete"):
            name = f"crm_{table}_{event}"
            op.execute(f"DROP TRIGGER {name} ON {table}")
            op.execute(f"DROP FUNCTION {name}()")
    op.execute("DROP INDEX ix_creative_material_dirty")
    op.drop_table("creative_material_entity_day")
