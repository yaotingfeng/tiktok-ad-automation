"""Immutable account membership versions and synchronous daily account totals."""

from alembic import op

revision = "0103_account_report_reads"
down_revision = "0102_business_rate_periods"
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
    op.execute("""
    CREATE TABLE account_membership_version (
        id UUID PRIMARY KEY, tenant_id UUID NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
        advertiser_id VARCHAR(128) NOT NULL, refs JSONB NOT NULL, naming_versions JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(), retired_at TIMESTAMPTZ,
        CONSTRAINT uq_account_membership_scope UNIQUE(tenant_id,advertiser_id,id)
    );
    CREATE INDEX ix_account_membership_retired ON account_membership_version(retired_at)
        WHERE retired_at IS NOT NULL;
    CREATE TABLE account_membership_current (
        tenant_id UUID NOT NULL,advertiser_id VARCHAR(128) NOT NULL,version_id UUID NOT NULL,
        PRIMARY KEY(tenant_id,advertiser_id),
        FOREIGN KEY(tenant_id,advertiser_id) REFERENCES advertiser_account(tenant_id,advertiser_id) ON DELETE CASCADE,
        FOREIGN KEY(tenant_id,advertiser_id,version_id)
          REFERENCES account_membership_version(tenant_id,advertiser_id,id)
    );
    CREATE INDEX ix_account_membership_current_version ON account_membership_current(version_id);
    CREATE FUNCTION qrm_membership_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
        IF (NEW.id,NEW.tenant_id,NEW.advertiser_id,NEW.refs,NEW.naming_versions,NEW.created_at)
           IS DISTINCT FROM
           (OLD.id,OLD.tenant_id,OLD.advertiser_id,OLD.refs,OLD.naming_versions,OLD.created_at) THEN
            RAISE EXCEPTION 'account membership payload is immutable';
        END IF;
        RETURN NEW;
    END $$;
    CREATE TRIGGER qrm_membership_immutable BEFORE UPDATE ON account_membership_version
        FOR EACH ROW EXECUTE FUNCTION qrm_membership_immutable();
    CREATE FUNCTION qrm_refresh_account_membership(t UUID,a TEXT) RETURNS void LANGUAGE plpgsql AS $$
    DECLARE old_id UUID; new_id UUID; new_refs JSONB; new_names JSONB;
    BEGIN
        IF NOT EXISTS(SELECT 1 FROM advertiser_account WHERE tenant_id=t AND advertiser_id=a) THEN RETURN; END IF;
        PERFORM pg_advisory_xact_lock(hashtextextended('account-membership:'||t::text||':'||a,0));
        SELECT version_id INTO old_id FROM account_membership_current
            WHERE tenant_id=t AND advertiser_id=a FOR UPDATE;
        SELECT COALESCE(jsonb_agg(jsonb_build_object('tenant_id',t::text,'advertiser_id',a,
                   'kind','campaign','remote_id',e.remote_id) ORDER BY e.remote_id COLLATE "C"),'[]'::jsonb),
               COALESCE(jsonb_object_agg(a||':'||e.remote_id,n.name_revision)
                   FILTER(WHERE n.name_revision IS NOT NULL),'{}'::jsonb)
        INTO new_refs,new_names
        FROM ad_object e LEFT JOIN campaign_name_current n
          ON n.tenant_id=e.tenant_id AND n.advertiser_id=e.advertiser_id AND n.campaign_remote_id=e.remote_id
        WHERE e.tenant_id=t AND e.advertiser_id=a AND e.kind='campaign';
        IF old_id IS NOT NULL AND EXISTS(SELECT 1 FROM account_membership_version
            WHERE id=old_id AND refs=new_refs AND naming_versions=new_names) THEN RETURN; END IF;
        new_id:=gen_random_uuid();
        INSERT INTO account_membership_version(id,tenant_id,advertiser_id,refs,naming_versions)
            VALUES(new_id,t,a,new_refs,new_names);
        UPDATE account_membership_version SET retired_at=clock_timestamp() WHERE id=old_id;
        INSERT INTO account_membership_current VALUES(t,a,new_id)
            ON CONFLICT(tenant_id,advertiser_id) DO UPDATE SET version_id=excluded.version_id;
    END $$;
    """)
    # 原qrm触发器先发布窄目录/名称，再按固定账户顺序更新版本；同一事务对外可见。
    for table in ("ad_object", "campaign_name_projection", "advertiser_account"):
        operations = (
            ("INSERT",)
            if table == "advertiser_account"
            else ("INSERT", "UPDATE", "DELETE")
        )
        for operation in operations:
            aliases = (
                ["new_rows"]
                if operation == "INSERT"
                else ["old_rows"]
                if operation == "DELETE"
                else ["old_rows", "new_rows"]
            )
            predicate = " WHERE kind='campaign'" if table == "ad_object" else ""
            keys = " UNION ".join(
                f"SELECT tenant_id,advertiser_id FROM {alias}{predicate}"
                for alias in aliases
            )
            if table == "ad_object" and operation == "UPDATE":
                # 预算/开关/名称显示变化不改变系列管理身份，不能每轮状态回读重建成员JSON。
                keys = """SELECT COALESCE(o.tenant_id,n.tenant_id) AS tenant_id,
                    COALESCE(o.advertiser_id,n.advertiser_id) AS advertiser_id
                    FROM old_rows o FULL JOIN new_rows n USING(tenant_id,advertiser_id,kind,remote_id)
                    WHERE (o.remote_id IS NULL OR n.remote_id IS NULL)
                      AND COALESCE(o.kind,n.kind)='campaign'"""
            transitions = " ".join(
                "OLD TABLE AS old_rows"
                if alias == "old_rows"
                else "NEW TABLE AS new_rows"
                for alias in aliases
            )
            name = f"zz_account_membership_{table}_{operation.lower()}"
            op.execute(f"""CREATE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $$
                DECLARE k RECORD; BEGIN
                FOR k IN SELECT DISTINCT tenant_id,advertiser_id FROM ({keys}) keys
                    ORDER BY tenant_id,advertiser_id LOOP
                    PERFORM qrm_refresh_account_membership(k.tenant_id,k.advertiser_id);
                END LOOP; RETURN NULL; END $$;
                CREATE TRIGGER {name} AFTER {operation} ON {table} REFERENCING {transitions}
                    FOR EACH STATEMENT EXECUTE FUNCTION {name}();""")
    op.execute("""
    CREATE FUNCTION zz_account_membership_retire() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
        UPDATE account_membership_version v SET retired_at=clock_timestamp()
        FROM old_rows o WHERE v.tenant_id=o.tenant_id AND v.advertiser_id=o.advertiser_id AND v.retired_at IS NULL;
        RETURN NULL;
    END $$;
    CREATE TRIGGER zz_account_membership_retire AFTER DELETE ON advertiser_account
        REFERENCING OLD TABLE AS old_rows FOR EACH STATEMENT EXECUTE FUNCTION zz_account_membership_retire();
    -- 迁移已冻结源写入且目标表为空；集合初始化不调用运行时维护函数，
    -- 避免每账户事务级advisory锁一直累计到整条升级链提交。
    WITH created AS (
        INSERT INTO account_membership_version(id,tenant_id,advertiser_id,refs,naming_versions)
        SELECT gen_random_uuid(),a.tenant_id,a.advertiser_id,
            COALESCE(jsonb_agg(jsonb_build_object('tenant_id',a.tenant_id::text,
                'advertiser_id',a.advertiser_id,'kind','campaign','remote_id',e.remote_id)
                ORDER BY e.remote_id COLLATE "C") FILTER(WHERE e.remote_id IS NOT NULL),'[]'::jsonb),
            COALESCE(jsonb_object_agg(a.advertiser_id||':'||e.remote_id,n.name_revision)
                FILTER(WHERE n.name_revision IS NOT NULL),'{}'::jsonb)
        FROM advertiser_account a LEFT JOIN ad_object e
          ON e.tenant_id=a.tenant_id AND e.advertiser_id=a.advertiser_id AND e.kind='campaign'
        LEFT JOIN campaign_name_current n ON n.tenant_id=e.tenant_id
          AND n.advertiser_id=e.advertiser_id AND n.campaign_remote_id=e.remote_id
        GROUP BY a.tenant_id,a.advertiser_id
        RETURNING id,tenant_id,advertiser_id
    )
    INSERT INTO account_membership_current(tenant_id,advertiser_id,version_id)
        SELECT tenant_id,advertiser_id,id FROM created;
    ALTER TABLE query_snapshot_row ADD COLUMN account_membership_version_id UUID
        REFERENCES account_membership_version(id);
    CREATE INDEX ix_query_snapshot_row_account_membership_version_id
        ON query_snapshot_row(account_membership_version_id) WHERE account_membership_version_id IS NOT NULL;
    -- 旧查询保留原始成员与原名称证据，绝不换成当前目录。
    DO $$ DECLARE r RECORD; version UUID; names JSONB; BEGIN
        FOR r IN SELECT q.snapshot_id,q.row_key,q.refs,s.tenant_id,s.naming_versions
          FROM query_snapshot_row q JOIN query_snapshot s ON s.id=q.snapshot_id
          WHERE q.dimension='account' LOOP
            version:=gen_random_uuid();
            SELECT COALESCE(jsonb_object_agg((e->>'advertiser_id')||':'||(e->>'remote_id'),
                       r.naming_versions->((e->>'advertiser_id')||':'||(e->>'remote_id')))
                       FILTER(WHERE r.naming_versions ? ((e->>'advertiser_id')||':'||(e->>'remote_id'))),'{}'::jsonb)
              INTO names FROM jsonb_array_elements(r.refs) e;
            INSERT INTO account_membership_version(id,tenant_id,advertiser_id,refs,naming_versions,retired_at)
              VALUES(version,r.tenant_id,split_part(r.row_key,':account:',1),r.refs,names,clock_timestamp());
            UPDATE query_snapshot_row SET refs='[]'::jsonb,account_membership_version_id=version
              WHERE snapshot_id=r.snapshot_id AND row_key=r.row_key;
        END LOOP;
        UPDATE query_snapshot s SET naming_versions=(SELECT jsonb_build_object('directory',1)||
            COALESCE(jsonb_object_agg('account-version:'||q.account_membership_version_id::text,1),'{}'::jsonb)
            FROM query_snapshot_row q WHERE q.snapshot_id=s.id)
          WHERE s.filters->>'dimension'='account';
    END $$;
    ALTER TABLE query_snapshot_row ADD CONSTRAINT ck_snapshot_account_membership CHECK (
        (dimension='account' AND account_membership_version_id IS NOT NULL AND refs='[]'::jsonb) OR
        (dimension<>'account' AND account_membership_version_id IS NULL));
    """)
    columns = ",".join(f"{m} NUMERIC,{m}_has_null BOOLEAN NOT NULL" for m in METRICS)
    op.execute(f"""
    CREATE TABLE account_report_day_state (
        tenant_id UUID NOT NULL,advertiser_id VARCHAR(128) NOT NULL,stat_date DATE NOT NULL,
        revision BIGINT NOT NULL DEFAULT 0,status VARCHAR(16),observed_at TIMESTAMPTZ,last_success_at TIMESTAMPTZ,
        PRIMARY KEY(tenant_id,advertiser_id,stat_date),
        FOREIGN KEY(tenant_id,advertiser_id) REFERENCES advertiser_account(tenant_id,advertiser_id) ON DELETE CASCADE);
    CREATE INDEX ix_account_report_state_period ON account_report_day_state(tenant_id,stat_date,advertiser_id);
    CREATE TABLE account_report_day (
        tenant_id UUID NOT NULL,advertiser_id VARCHAR(128) NOT NULL,stat_date DATE NOT NULL,
        currency VARCHAR(16) NOT NULL,timezone VARCHAR(64) NOT NULL,{columns},
        fetched_at TIMESTAMPTZ NOT NULL,source_revision BIGINT NOT NULL,
        PRIMARY KEY(tenant_id,advertiser_id,stat_date,currency,timezone),
        FOREIGN KEY(tenant_id,advertiser_id) REFERENCES advertiser_account(tenant_id,advertiser_id) ON DELETE CASCADE);
    CREATE INDEX ix_account_report_day_period ON account_report_day(tenant_id,stat_date,advertiser_id);
    """)
    names = ",".join(
        name for metric in METRICS for name in (metric, f"{metric}_has_null")
    )
    values = ",".join(f"SUM(f.{m}),bool_or(f.{m} IS NULL)" for m in METRICS)
    op.execute(f"""
    CREATE FUNCTION qrm_refresh_account_day(t UUID,a TEXT,d DATE) RETURNS void LANGUAGE plpgsql AS $$
    DECLARE rev BIGINT; BEGIN
        IF NOT EXISTS(SELECT 1 FROM advertiser_account WHERE tenant_id=t AND advertiser_id=a) THEN RETURN; END IF;
        -- 即使完整空桶也保留写锁行，使RR旧事务不能覆盖后来提交的汇总。
        INSERT INTO account_report_day_state(tenant_id,advertiser_id,stat_date,revision)
          VALUES(t,a,d,1) ON CONFLICT(tenant_id,advertiser_id,stat_date)
          DO UPDATE SET revision=account_report_day_state.revision+1 RETURNING revision INTO rev;
        DELETE FROM account_report_day WHERE tenant_id=t AND advertiser_id=a AND stat_date=d;
        INSERT INTO account_report_day(tenant_id,advertiser_id,stat_date,currency,timezone,
            {names},fetched_at,source_revision)
          SELECT t,a,d,f.currency,f.timezone,{values},MAX(f.fetched_at),rev
          FROM campaign_report_day f WHERE f.tenant_id=t AND f.advertiser_id=a AND f.stat_date=d
          GROUP BY f.currency,f.timezone;
    END $$;
    """)
    for table in ("campaign_report_day", "report_sync_state"):
        for operation in ("INSERT", "UPDATE", "DELETE"):
            aliases = (
                ["new_rows"]
                if operation == "INSERT"
                else ["old_rows"]
                if operation == "DELETE"
                else ["old_rows", "new_rows"]
            )
            predicate = (
                " WHERE report_kind='ad_day'" if table == "report_sync_state" else ""
            )
            keys = " UNION ".join(
                f"SELECT tenant_id,advertiser_id,stat_date FROM {alias}{predicate}"
                for alias in aliases
            )
            transitions = " ".join(
                "OLD TABLE AS old_rows"
                if alias == "old_rows"
                else "NEW TABLE AS new_rows"
                for alias in aliases
            )
            name = f"zz_account_day_{table}_{operation.lower()}"
            action = "PERFORM qrm_refresh_account_day(k.tenant_id,k.advertiser_id,k.stat_date);"
            if table == "report_sync_state":
                action = """
                INSERT INTO account_report_day_state(tenant_id,advertiser_id,stat_date,status,observed_at,last_success_at)
                  SELECT k.tenant_id,k.advertiser_id,k.stat_date,s.status,s.observed_at,s.last_success_at
                  FROM (SELECT 1) singleton LEFT JOIN report_sync_state s ON s.tenant_id=k.tenant_id
                    AND s.advertiser_id=k.advertiser_id AND s.stat_date=k.stat_date AND s.report_kind='ad_day'
                  WHERE EXISTS(SELECT 1 FROM advertiser_account WHERE tenant_id=k.tenant_id AND advertiser_id=k.advertiser_id)
                  ON CONFLICT(tenant_id,advertiser_id,stat_date) DO UPDATE SET
                    status=excluded.status,observed_at=excluded.observed_at,last_success_at=excluded.last_success_at;
                """
            op.execute(f"""CREATE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $$
                DECLARE k RECORD; BEGIN
                FOR k IN SELECT DISTINCT tenant_id,advertiser_id,stat_date FROM ({keys}) keys
                    ORDER BY tenant_id,advertiser_id,stat_date LOOP {action} END LOOP; RETURN NULL; END $$;
                CREATE TRIGGER {name} AFTER {operation} ON {table} REFERENCING {transitions}
                    FOR EACH STATEMENT EXECUTE FUNCTION {name}();""")

    def restore_statements(accounts: str) -> tuple[str, str, str]:
        # accounts仅为固定表名/transition表，不含调用者输入。0081保留已删账户的
        # canonical历史；回填先约束存活账户，重新发现时再从原历史恢复派生数据。
        return (
            f"""INSERT INTO account_report_day_state(tenant_id,advertiser_id,stat_date,revision)
                SELECT f.tenant_id,f.advertiser_id,f.stat_date,1 FROM campaign_report_day f
                JOIN {accounts} a USING(tenant_id,advertiser_id)
                GROUP BY f.tenant_id,f.advertiser_id,f.stat_date
                ORDER BY f.tenant_id,f.advertiser_id,f.stat_date
                ON CONFLICT(tenant_id,advertiser_id,stat_date)
                DO UPDATE SET revision=account_report_day_state.revision+1""",
            f"""INSERT INTO account_report_day(tenant_id,advertiser_id,stat_date,currency,timezone,
                    {names},fetched_at,source_revision)
                SELECT f.tenant_id,f.advertiser_id,f.stat_date,f.currency,f.timezone,
                    {values},MAX(f.fetched_at),h.revision
                FROM campaign_report_day f JOIN {accounts} a USING(tenant_id,advertiser_id)
                JOIN account_report_day_state h ON h.tenant_id=f.tenant_id
                    AND h.advertiser_id=f.advertiser_id AND h.stat_date=f.stat_date
                GROUP BY f.tenant_id,f.advertiser_id,f.stat_date,f.currency,f.timezone,h.revision""",
            f"""INSERT INTO account_report_day_state
                    (tenant_id,advertiser_id,stat_date,status,observed_at,last_success_at)
                SELECT s.tenant_id,s.advertiser_id,s.stat_date,s.status,s.observed_at,s.last_success_at
                FROM report_sync_state s JOIN {accounts} a USING(tenant_id,advertiser_id)
                WHERE s.report_kind='ad_day' ORDER BY s.tenant_id,s.advertiser_id,s.stat_date
                ON CONFLICT(tenant_id,advertiser_id,stat_date) DO UPDATE SET
                    status=excluded.status,observed_at=excluded.observed_at,last_success_at=excluded.last_success_at""",
        )

    # 冻结窗口内全量集合回填，避免逐账户日反复随机读取历史；修订与金额同批落库。
    for statement in restore_statements("advertiser_account"):
        op.execute(statement)
    # 账户重新发现时，已保留的历史金额/状态必须立即恢复；新账户通常没有历史。
    # 大小批次混用transition表时逐语句重规划，防止首批空表计划污染后续大导入。
    restore = ";".join(
        f"EXECUTE $restore_{i}${statement}$restore_{i}$"
        for i, statement in enumerate(restore_statements("new_rows"))
    )
    op.execute(f"""CREATE FUNCTION zz_account_day_advertiser_insert() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            IF NOT EXISTS(SELECT 1 FROM new_rows) THEN RETURN NULL; END IF;
            {restore}; RETURN NULL;
        END $$;
        CREATE TRIGGER zz_account_day_advertiser_insert AFTER INSERT ON advertiser_account
          REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT
          EXECUTE FUNCTION zz_account_day_advertiser_insert();""")


def downgrade() -> None:
    # 逆迁移先展开仍存活的账户快照；选择/导出早已保存完整证据，无需改写。
    op.execute("""
    ALTER TABLE query_snapshot_row DROP CONSTRAINT ck_snapshot_account_membership;
    UPDATE query_snapshot_row q SET refs=v.refs FROM account_membership_version v
      WHERE q.account_membership_version_id=v.id;
    UPDATE query_snapshot s SET naming_versions=(SELECT jsonb_build_object('directory',1)||
      COALESCE(jsonb_object_agg(n.key,n.value),'{}'::jsonb)
      FROM query_snapshot_row q JOIN account_membership_version v ON v.id=q.account_membership_version_id
      CROSS JOIN LATERAL jsonb_each(v.naming_versions) n WHERE q.snapshot_id=s.id)
      WHERE s.filters->>'dimension'='account';
    ALTER TABLE query_snapshot_row DROP COLUMN account_membership_version_id;
    """)
    for table in ("campaign_report_day", "report_sync_state"):
        for operation in ("insert", "update", "delete"):
            name = f"zz_account_day_{table}_{operation}"
            op.execute(f"DROP TRIGGER {name} ON {table}; DROP FUNCTION {name}()")
    for table in ("ad_object", "campaign_name_projection", "advertiser_account"):
        operations = (
            ("insert",)
            if table == "advertiser_account"
            else ("insert", "update", "delete")
        )
        for operation in operations:
            name = f"zz_account_membership_{table}_{operation}"
            op.execute(f"DROP TRIGGER {name} ON {table}; DROP FUNCTION {name}()")
    op.execute("""
    DROP TRIGGER zz_account_day_advertiser_insert ON advertiser_account;
    DROP FUNCTION zz_account_day_advertiser_insert();
    DROP TRIGGER zz_account_membership_retire ON advertiser_account;
    DROP FUNCTION zz_account_membership_retire();
    DROP FUNCTION qrm_refresh_account_day(UUID,TEXT,DATE);
    DROP TABLE account_report_day,account_report_day_state;
    DROP FUNCTION qrm_refresh_account_membership(UUID,TEXT);
    DROP TABLE account_membership_current,account_membership_version;
    DROP FUNCTION qrm_membership_immutable();
    """)
