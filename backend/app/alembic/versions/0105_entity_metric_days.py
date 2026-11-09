"""Synchronous compact metric facts for all entity dimensions."""

from alembic import op

revision = "0105_entity_metric_days"
down_revision = "0103_account_report_reads"
branch_labels = None
depends_on = None

# 固化迁移合同；不依赖后来可能改变的业务模型或指标清单。
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
SOURCES = (
    ("campaign", "campaign_report_day", "campaign_id"),
    ("adgroup", "adgroup_report_day", "adgroup_id"),
    ("ad", "ad_report_day", "ad_id"),
    ("material", "material_entity_day", "row_key"),
)
KEYS = (
    "tenant_id",
    "advertiser_id",
    "stat_date",
    "kind",
    "remote_id",
    "variant",
    "currency",
    "timezone",
)
VALUES = tuple(name for m in METRICS for name in (m, f"{m}_has_null"))
COLUMNS = KEYS + VALUES + ("fetched_at", "source_revision")


def _projection(kind: str, identity: str, source: str) -> str:
    partial = kind in {"adgroup", "material"}
    variant = "f.main_material_type" if kind == "material" else "''::varchar"
    revision_sql = "f.source_revision" if partial else "txid_current()"
    metrics = ",".join(
        f"f.{m},"
        + (f"(f.{m}_has_null OR f.{m} IS NULL)" if partial else f"(f.{m} IS NULL)")
        + f" AS {m}_has_null"
        for m in METRICS
    )
    return f"""SELECT f.tenant_id,f.advertiser_id,f.stat_date,'{kind}'::varchar AS kind,
        f.{identity} AS remote_id,{variant} AS variant,f.currency,f.timezone,
        {metrics},f.fetched_at,{revision_sql} AS source_revision FROM {source} f"""


def _upsert(query: str) -> str:
    columns = ",".join(COLUMNS)
    updates = ",".join(
        f"{c}=excluded.{c}" for c in VALUES + ("fetched_at", "source_revision")
    )
    old = ",".join(f"entity_metric_day.{c}" for c in VALUES)
    new = ",".join(f"excluded.{c}" for c in VALUES)
    return f"""INSERT INTO entity_metric_day ({columns}) {query}
        ON CONFLICT ({",".join(KEYS)}) DO UPDATE SET {updates}
        WHERE ({old}) IS DISTINCT FROM ({new})"""


def _restore(accounts: str, kind: str, table: str, identity: str) -> str:
    return _upsert(
        _projection(kind, identity, table)
        + f"""
        JOIN {accounts} a USING(tenant_id,advertiser_id)
        ORDER BY f.tenant_id,f.advertiser_id,f.stat_date,f.{identity},f.currency,f.timezone"""
    )


def upgrade() -> None:
    metrics = ",".join(f"{m} NUMERIC,{m}_has_null BOOLEAN NOT NULL" for m in METRICS)
    op.execute(f"""
        CREATE TABLE entity_metric_day (
            tenant_id UUID NOT NULL,advertiser_id VARCHAR(128) NOT NULL,stat_date DATE NOT NULL,
            kind VARCHAR(16) NOT NULL,remote_id VARCHAR(512) NOT NULL,variant VARCHAR(64) NOT NULL,
            currency VARCHAR(16) NOT NULL,timezone VARCHAR(64) NOT NULL,
            {metrics},fetched_at TIMESTAMPTZ NOT NULL,source_revision BIGINT NOT NULL,
            PRIMARY KEY ({",".join(KEYS)}),
            FOREIGN KEY(tenant_id,advertiser_id)
                REFERENCES advertiser_account(tenant_id,advertiser_id) ON DELETE CASCADE,
            CONSTRAINT ck_entity_metric_day_kind CHECK(kind IN ('campaign','adgroup','ad','material')),
            CONSTRAINT ck_entity_metric_day_variant CHECK(kind='material' OR variant='')
        ) PARTITION BY RANGE (stat_date);
        CREATE INDEX ix_entity_metric_day_period ON entity_metric_day(tenant_id,kind,stat_date,advertiser_id);
        CREATE TABLE entity_metric_day_default PARTITION OF entity_metric_day DEFAULT;
        CREATE INDEX ix_entity_metric_day_default_date ON entity_metric_day_default(stat_date);
    """)
    # 只为真实历史月份及当前/未来三个月建分区；长时间无数据的月份不占目录。
    months = " UNION ".join(
        f"SELECT DISTINCT date_trunc('month',f.stat_date)::date AS month FROM {table} f "
        "JOIN advertiser_account a USING(tenant_id,advertiser_id)"
        for _, table, _ in SOURCES
    )
    op.execute(f"""DO $$ DECLARE month DATE; BEGIN
        FOR month IN SELECT q.month FROM ({months}
            UNION SELECT (date_trunc('month',CURRENT_DATE)+make_interval(months=>i))::date
                FROM generate_series(0,3) i) q ORDER BY q.month LOOP
            EXECUTE format('CREATE TABLE %I PARTITION OF entity_metric_day FOR VALUES FROM (%L) TO (%L)',
                'entity_metric_day_'||to_char(month,'YYYY_MM'),month,(month+interval '1 month')::date);
        END LOOP; END $$;""")
    for kind, table, identity in SOURCES:
        op.execute(_restore("advertiser_account", kind, table, identity))
        # 以整个statement为单位复制，只看金额、NULL旗标及完整身份；名称、同步
        # 时间和源修订单独变化不制造紧凑表写放大。新鲜度合同继续使用同步状态表。
        for operation in ("insert", "update", "delete"):
            name = f"qem_{kind}_{operation}"
            statements = []
            old_projection = _projection(kind, identity, "old_rows")
            new_projection = _projection(kind, identity, "new_rows")
            if operation in {"update", "delete"}:
                predicate = " AND ".join(f"d.{c}=o.{c}" for c in KEYS)
                keep = ""
                if operation == "update":
                    match = " AND ".join(f"n.{c}=o.{c}" for c in KEYS)
                    keep = f"AND NOT EXISTS(SELECT 1 FROM ({new_projection}) n WHERE {match})"
                statements.append(
                    f"DELETE FROM entity_metric_day d USING ({old_projection}) o WHERE {predicate} {keep}"
                )
            if operation in {"insert", "update"}:
                changes = new_projection
                if operation == "update":
                    old_values = ",".join(f"o.{c}" for c in VALUES)
                    new_values = ",".join(f"n.{c}" for c in VALUES)
                    changes = f"""SELECT n.* FROM ({new_projection}) n
                        LEFT JOIN ({old_projection}) o USING({",".join(KEYS)})
                        WHERE o.remote_id IS NULL OR ({old_values}) IS DISTINCT FROM ({new_values})"""
                selected = ",".join(f"n.{c}" for c in COLUMNS)
                query = f"""SELECT {selected} FROM ({changes}) n
                    JOIN advertiser_account a USING(tenant_id,advertiser_id)
                    ORDER BY {",".join("n." + c for c in KEYS)}"""
                statements.append(_upsert(query))
            transitions = (
                "NEW TABLE AS new_rows"
                if operation == "insert"
                else "OLD TABLE AS old_rows"
                if operation == "delete"
                else "OLD TABLE AS old_rows NEW TABLE AS new_rows"
            )
            # transition基数可能从单行变成整日批次，动态SQL强制按实际批量重规划。
            body = ";".join(f"EXECUTE $qem${s}$qem$" for s in statements)
            op.execute(f"""CREATE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN {body}; RETURN NULL; END $$;
                CREATE TRIGGER {name} AFTER {operation.upper()} ON {table}
                    REFERENCING {transitions} FOR EACH STATEMENT EXECUTE FUNCTION {name}();""")
        id_columns = f"remote_id AS {identity}"
        if kind == "material":
            id_columns += ",variant AS main_material_type"
        columns = ",".join(VALUES)
        op.execute(f"""CREATE VIEW {kind}_metric_day AS SELECT tenant_id,advertiser_id,stat_date,
            {id_columns},currency,timezone,{columns},fetched_at,source_revision
            FROM entity_metric_day WHERE kind='{kind}'""")
    # 删除账户由FK只清派生事实；重新发现后立即恢复仍保留的canonical及派生源。
    restore = ";".join(
        f"EXECUTE $restore${_restore('new_rows', kind, table, identity)}$restore$"
        for kind, table, identity in SOURCES
    )
    op.execute(f"""CREATE FUNCTION qem_advertiser_insert() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS(SELECT 1 FROM new_rows) THEN RETURN NULL; END IF;
            {restore}; RETURN NULL;
        END $$;
        CREATE TRIGGER qem_advertiser_insert AFTER INSERT ON advertiser_account
            REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION qem_advertiser_insert();""")


def downgrade() -> None:
    # 全部对象都可重建；逆迁移不改canonical金额、名称或同步覆盖证据。
    op.execute(
        "DROP TRIGGER qem_advertiser_insert ON advertiser_account; DROP FUNCTION qem_advertiser_insert()"
    )
    for kind, table, _ in SOURCES:
        op.execute(f"DROP VIEW {kind}_metric_day")
        for operation in ("insert", "update", "delete"):
            name = f"qem_{kind}_{operation}"
            op.execute(f"DROP TRIGGER {name} ON {table}; DROP FUNCTION {name}()")
    op.execute("DROP TABLE entity_metric_day")
