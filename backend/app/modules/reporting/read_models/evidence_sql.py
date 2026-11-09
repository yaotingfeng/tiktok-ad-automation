"""不可变快照证据DDL模板；迁移保存展开后的固定SQL。"""


def evidence_statements() -> list[str]:
    result = [
        """CREATE TABLE report_evidence_version (
            id UUID PRIMARY KEY,tenant_id UUID NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
            advertiser_id VARCHAR(128) NOT NULL,kind VARCHAR(32) NOT NULL,
            refs JSONB NOT NULL DEFAULT '[]',material_uses JSONB NOT NULL DEFAULT '[]',
            naming_versions JSONB NOT NULL DEFAULT '{}',created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            retired_at TIMESTAMPTZ,CONSTRAINT uq_report_evidence_scope UNIQUE(tenant_id,advertiser_id,id))""",
        "CREATE INDEX ix_report_evidence_retired ON report_evidence_version(retired_at) WHERE retired_at IS NOT NULL",
        """CREATE FUNCTION qrm_report_evidence_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
            IF (NEW.id,NEW.tenant_id,NEW.advertiser_id,NEW.kind,NEW.refs,NEW.material_uses,NEW.naming_versions,NEW.created_at)
              IS DISTINCT FROM (OLD.id,OLD.tenant_id,OLD.advertiser_id,OLD.kind,OLD.refs,OLD.material_uses,OLD.naming_versions,OLD.created_at)
            THEN RAISE EXCEPTION 'report evidence is immutable'; END IF; RETURN NEW;
        END $$""",
        "CREATE TRIGGER qrm_report_evidence_immutable BEFORE UPDATE ON report_evidence_version FOR EACH ROW EXECUTE FUNCTION qrm_report_evidence_immutable()",
        """CREATE TABLE report_name_scope_current (
            tenant_id UUID NOT NULL,advertiser_id VARCHAR(128) NOT NULL,version_id UUID NOT NULL,
            PRIMARY KEY(tenant_id,advertiser_id),
            FOREIGN KEY(tenant_id,advertiser_id) REFERENCES advertiser_account(tenant_id,advertiser_id) ON DELETE CASCADE,
            FOREIGN KEY(tenant_id,advertiser_id,version_id) REFERENCES report_evidence_version(tenant_id,advertiser_id,id))""",
        "CREATE INDEX ix_report_name_scope_version ON report_name_scope_current(version_id)",
        """CREATE TABLE query_snapshot_evidence (
            snapshot_id UUID NOT NULL,row_key VARCHAR(512) NOT NULL,version_id UUID NOT NULL REFERENCES report_evidence_version(id),
            PRIMARY KEY(snapshot_id,row_key,version_id),
            FOREIGN KEY(snapshot_id,row_key) REFERENCES query_snapshot_row(snapshot_id,row_key) ON DELETE CASCADE)""",
        "CREATE INDEX ix_query_snapshot_evidence_version ON query_snapshot_evidence(version_id)",
        """CREATE TABLE query_snapshot_name_version (
            snapshot_id UUID NOT NULL REFERENCES query_snapshot(id) ON DELETE CASCADE,
            version_id UUID NOT NULL REFERENCES report_evidence_version(id),PRIMARY KEY(snapshot_id,version_id))""",
        "CREATE INDEX ix_query_snapshot_name_version_version ON query_snapshot_name_version(version_id)",
        """CREATE FUNCTION qrm_refresh_report_names(t UUID,a TEXT) RETURNS void LANGUAGE plpgsql AS $$
        DECLARE old_id UUID; next_id UUID; names JSONB;
        BEGIN
            IF NOT EXISTS(SELECT 1 FROM advertiser_account WHERE tenant_id=t AND advertiser_id=a) THEN RETURN; END IF;
            PERFORM pg_advisory_xact_lock(hashtextextended('report-name-scope:'||t::text||':'||a,0));
            SELECT version_id INTO old_id FROM report_name_scope_current WHERE tenant_id=t AND advertiser_id=a FOR UPDATE;
            SELECT COALESCE(jsonb_object_agg(a||':'||campaign_remote_id,name_revision),'{}') INTO names
              FROM campaign_name_current WHERE tenant_id=t AND advertiser_id=a;
            IF old_id IS NOT NULL AND EXISTS(SELECT 1 FROM report_evidence_version WHERE id=old_id AND naming_versions=names) THEN RETURN; END IF;
            next_id:=gen_random_uuid();
            INSERT INTO report_evidence_version(id,tenant_id,advertiser_id,kind,naming_versions)
              VALUES(next_id,t,a,'name_scope',names);
            INSERT INTO report_name_scope_current VALUES(t,a,next_id) ON CONFLICT(tenant_id,advertiser_id)
              DO UPDATE SET version_id=excluded.version_id;
            UPDATE report_evidence_version SET retired_at=clock_timestamp() WHERE id=old_id;
        END $$""",
        """CREATE FUNCTION qrm_retire_report_names() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
            UPDATE report_evidence_version SET retired_at=COALESCE(retired_at,clock_timestamp()) WHERE id=OLD.version_id;
            RETURN OLD; END $$""",
        "CREATE TRIGGER qrm_retire_report_names AFTER DELETE ON report_name_scope_current FOR EACH ROW EXECUTE FUNCTION qrm_retire_report_names()",
    ]
    for table in ("campaign_name_projection", "advertiser_account"):
        operations = (
            ("insert",)
            if table == "advertiser_account"
            else ("insert", "update", "delete")
        )
        for operation in operations:
            aliases = (
                ("new_rows",)
                if operation == "insert"
                else ("old_rows",)
                if operation == "delete"
                else ("old_rows", "new_rows")
            )
            keys = " UNION ".join(
                f"SELECT tenant_id,advertiser_id FROM {a}" for a in aliases
            )
            transitions = " ".join(
                "OLD TABLE AS old_rows" if a == "old_rows" else "NEW TABLE AS new_rows"
                for a in aliases
            )
            function = f"zzz_report_names_{table}_{operation}"
            result.append(f"""CREATE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS $$
                DECLARE k RECORD; BEGIN
                FOR k IN SELECT DISTINCT tenant_id,advertiser_id FROM ({keys}) k ORDER BY tenant_id,advertiser_id LOOP
                    PERFORM qrm_refresh_report_names(k.tenant_id,k.advertiser_id);
                END LOOP; RETURN NULL; END $$""")
            result.append(
                f"CREATE TRIGGER {function} AFTER {operation} ON {table} REFERENCING {transitions} FOR EACH STATEMENT EXECUTE FUNCTION {function}()"
            )
    result.append("""DO $$ DECLARE k RECORD; BEGIN
        FOR k IN SELECT tenant_id,advertiser_id FROM advertiser_account ORDER BY tenant_id,advertiser_id LOOP
            PERFORM qrm_refresh_report_names(k.tenant_id,k.advertiser_id);
        END LOOP; END $$""")
    return result


LEGACY_UPGRADE_SQL = """
ALTER TABLE query_snapshot_row ADD COLUMN member_keys JSONB NOT NULL DEFAULT '[]';
ALTER TABLE query_snapshot_row ADD COLUMN member_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE query_snapshot_row DROP CONSTRAINT ck_snapshot_account_membership;
-- 单对象由固有键表达身份，剧冻结紧凑二元组，素材把旧证据拆成账户版本；不读当前目录。
DO $$ DECLARE r RECORD; adv TEXT; next_id UUID; named JSONB; frozen_refs JSONB; frozen_uses JSONB; BEGIN
  FOR r IN SELECT q.*,s.tenant_id FROM query_snapshot_row q JOIN query_snapshot s ON s.id=q.snapshot_id
      WHERE q.dimension<>'account' LOOP
    IF r.dimension='drama' THEN
      UPDATE query_snapshot_row SET member_keys=(SELECT COALESCE(jsonb_agg(jsonb_build_array(v->>'advertiser_id',v->>'remote_id') ORDER BY ord),'[]')
        FROM jsonb_array_elements(r.refs) WITH ORDINALITY x(v,ord)),member_count=jsonb_array_length(r.refs)
        WHERE snapshot_id=r.snapshot_id AND row_key=r.row_key;
    ELSIF r.dimension='material' THEN
      FOR adv IN SELECT DISTINCT v->>'advertiser_id' FROM jsonb_array_elements(r.refs) v
        UNION SELECT DISTINCT v->'ad_ref'->>'advertiser_id' FROM jsonb_array_elements(r.material_uses) v LOOP
        SELECT COALESCE(jsonb_agg(v ORDER BY ord),'[]') INTO frozen_refs
          FROM jsonb_array_elements(r.refs) WITH ORDINALITY x(v,ord) WHERE v->>'advertiser_id'=adv;
        SELECT COALESCE(jsonb_agg(v ORDER BY ord),'[]') INTO frozen_uses
          FROM jsonb_array_elements(r.material_uses) WITH ORDINALITY x(v,ord) WHERE v->'ad_ref'->>'advertiser_id'=adv;
        next_id:=gen_random_uuid();
        INSERT INTO report_evidence_version(id,tenant_id,advertiser_id,kind,refs,material_uses,retired_at)
          VALUES(next_id,r.tenant_id,adv,'material_snapshot',frozen_refs,frozen_uses,clock_timestamp());
        INSERT INTO query_snapshot_evidence VALUES(r.snapshot_id,r.row_key,next_id);
      END LOOP;
      UPDATE query_snapshot_row SET member_count=jsonb_array_length(r.material_uses) WHERE snapshot_id=r.snapshot_id AND row_key=r.row_key;
    ELSE
      IF r.refs <> jsonb_build_array(jsonb_build_object('tenant_id',r.tenant_id::text,
         'advertiser_id',split_part(r.row_key,':'||r.dimension||':',1),'kind',r.dimension,
         'remote_id',substring(r.row_key FROM length(split_part(r.row_key,':'||r.dimension||':',1))+length(r.dimension)+3))) THEN
        RAISE EXCEPTION 'scalar snapshot identity mismatch';
      END IF;
      UPDATE query_snapshot_row SET member_count=1 WHERE snapshot_id=r.snapshot_id AND row_key=r.row_key;
    END IF;
    UPDATE query_snapshot_row SET refs='[]',material_uses='[]' WHERE snapshot_id=r.snapshot_id AND row_key=r.row_key;
  END LOOP;
  FOR r IN SELECT * FROM query_snapshot WHERE filters->>'dimension' IN ('campaign','drama') LOOP
    FOR adv IN SELECT jsonb_array_elements_text(r.advertiser_ids) LOOP
      SELECT COALESCE(jsonb_object_agg(key,value),'{}') INTO named FROM jsonb_each(r.naming_versions)
        WHERE left(key,length(adv)+1)=adv||':';
      IF named='{}'::jsonb THEN CONTINUE; END IF;
      next_id:=gen_random_uuid();
      INSERT INTO report_evidence_version(id,tenant_id,advertiser_id,kind,naming_versions,retired_at)
        VALUES(next_id,r.tenant_id,adv,'name_scope',named,clock_timestamp());
      INSERT INTO query_snapshot_name_version VALUES(r.id,next_id);
    END LOOP;
    UPDATE query_snapshot SET naming_versions=(SELECT jsonb_build_object('directory',1)||COALESCE(jsonb_object_agg('name-version:'||version_id::text,1),'{}')
      FROM query_snapshot_name_version WHERE snapshot_id=r.id) WHERE id=r.id;
  END LOOP;
END $$;
ALTER TABLE query_snapshot_row ADD CONSTRAINT ck_snapshot_account_membership CHECK (
  (dimension='account' AND account_membership_version_id IS NOT NULL AND refs='[]'::jsonb) OR
  (dimension<>'account' AND account_membership_version_id IS NULL AND refs='[]'::jsonb AND material_uses='[]'::jsonb));
"""


LEGACY_DOWNGRADE_SQL = """
ALTER TABLE query_snapshot_row DROP CONSTRAINT ck_snapshot_account_membership;
UPDATE query_snapshot_row q SET refs=jsonb_build_array(jsonb_build_object('tenant_id',s.tenant_id::text,
    'advertiser_id',split_part(q.row_key,':'||q.dimension||':',1),'kind',q.dimension,
    'remote_id',substring(q.row_key FROM length(split_part(q.row_key,':'||q.dimension||':',1))+length(q.dimension)+3)))
  FROM query_snapshot s WHERE s.id=q.snapshot_id AND q.dimension IN ('campaign','adgroup','ad');
UPDATE query_snapshot_row q SET refs=(SELECT COALESCE(jsonb_agg(jsonb_build_object('tenant_id',s.tenant_id::text,
    'advertiser_id',v->>0,'kind','campaign','remote_id',v->>1) ORDER BY ord),'[]')
    FROM jsonb_array_elements(q.member_keys) WITH ORDINALITY x(v,ord))
  FROM query_snapshot s WHERE s.id=q.snapshot_id AND q.dimension='drama';
UPDATE query_snapshot_row q SET
  refs=(SELECT COALESCE(jsonb_agg(v ORDER BY v->>'advertiser_id' COLLATE "C",v->>'remote_id' COLLATE "C"),'[]')
    FROM (SELECT DISTINCT v FROM query_snapshot_evidence l JOIN report_evidence_version e ON e.id=l.version_id
      CROSS JOIN LATERAL jsonb_array_elements(e.refs) v WHERE l.snapshot_id=q.snapshot_id AND l.row_key=q.row_key) x),
  material_uses=(SELECT COALESCE(jsonb_agg(v ORDER BY v->'ad_ref'->>'advertiser_id' COLLATE "C",v->'ad_ref'->>'remote_id' COLLATE "C",
      v->>'platform_material_id' COLLATE "C",COALESCE(v->>'ad_material_id','') COLLATE "C",v->>'material_type' COLLATE "C"),'[]')
    FROM (SELECT DISTINCT v FROM query_snapshot_evidence l JOIN report_evidence_version e ON e.id=l.version_id
      CROSS JOIN LATERAL jsonb_array_elements(e.material_uses) v WHERE l.snapshot_id=q.snapshot_id AND l.row_key=q.row_key) x)
  WHERE q.dimension='material';
UPDATE query_snapshot s SET naming_versions=(SELECT jsonb_build_object('directory',1)||COALESCE(jsonb_object_agg(n.key,n.value),'{}')
  FROM query_snapshot_name_version l JOIN report_evidence_version e ON e.id=l.version_id
  CROSS JOIN LATERAL jsonb_each(e.naming_versions) n WHERE l.snapshot_id=s.id AND n.key IN (
    SELECT (v->>'advertiser_id')||':'||(v->>'remote_id') FROM query_snapshot_row q
    CROSS JOIN LATERAL jsonb_array_elements(q.refs) v WHERE q.snapshot_id=s.id AND v->>'kind'='campaign'))
  WHERE s.filters->>'dimension' IN ('campaign','drama');
ALTER TABLE query_snapshot_row DROP COLUMN member_keys,DROP COLUMN member_count;
ALTER TABLE query_snapshot_row ADD CONSTRAINT ck_snapshot_account_membership CHECK (
  (dimension='account' AND account_membership_version_id IS NOT NULL AND refs='[]'::jsonb) OR
  (dimension<>'account' AND account_membership_version_id IS NULL));
"""
