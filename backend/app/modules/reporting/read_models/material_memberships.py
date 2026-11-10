"""素材使用位置预存；迁移保存生成后的固定SQL，不动态导入此模板。"""

from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    Index,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlmodel import Field, SQLModel

from app.modules.reporting.read_models.models import account_reference


def _version_reference() -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", "advertiser_id", "version_id"],
        [
            "report_evidence_version.tenant_id",
            "report_evidence_version.advertiser_id",
            "report_evidence_version.id",
        ],
    )


class MaterialAdMemberCurrent(SQLModel, table=True):
    __tablename__ = "material_ad_member_current"
    __table_args__ = (
        account_reference(),
        _version_reference(),
        CheckConstraint("material_type='VIDEO'", name="ck_material_ad_member_video"),
        Index(
            "ix_material_ad_member_video",
            "tenant_id",
            "platform_material_id",
            "advertiser_id",
        ),
        Index("ix_material_ad_member_ad", "tenant_id", "advertiser_id", "ad_remote_id"),
        Index("ix_material_ad_member_version", "version_id"),
    )
    tenant_id: UUID = Field(primary_key=True)
    advertiser_id: str = Field(primary_key=True, max_length=128)
    ad_remote_id: str = Field(primary_key=True, max_length=128)
    platform_material_id: str = Field(primary_key=True, max_length=128)
    material_type: str = Field(primary_key=True, max_length=64)
    version_id: UUID
    ad_material_id: str | None = Field(default=None, max_length=128)
    name: str | None = None
    raw_id: UUID
    published_version: int = Field(sa_type=BigInteger)


class MaterialAccountMemberCurrent(SQLModel, table=True):
    __tablename__ = "material_account_member_current"
    __table_args__ = (
        account_reference(),
        _version_reference(),
        CheckConstraint("use_count>0", name="ck_material_account_member_count"),
        Index(
            "ix_material_account_member_video",
            "tenant_id",
            "platform_material_id",
            "advertiser_id",
            postgresql_include=["version_id", "use_count"],
        ),
        Index("ix_material_account_member_version", "version_id"),
    )
    tenant_id: UUID = Field(primary_key=True)
    advertiser_id: str = Field(primary_key=True, max_length=128)
    platform_material_id: str = Field(primary_key=True, max_length=128)
    version_id: UUID
    use_count: int = Field(sa_type=BigInteger)
    name: str | None = None
    ad_ids: list[str] = Field(
        default_factory=list, sa_column=Column(ARRAY(Text), nullable=False)
    )


def material_statements() -> list[str]:
    """账户删除只退休旧证据；canonical保持原有外键和删除合同。"""
    statements = [
        """CREATE TABLE material_ad_member_current (
            tenant_id uuid NOT NULL, advertiser_id varchar(128) NOT NULL,
            ad_remote_id varchar(128) NOT NULL, platform_material_id varchar(128) NOT NULL,
            material_type varchar(64) NOT NULL CHECK (material_type='VIDEO'),
            version_id uuid NOT NULL, ad_material_id varchar(128), name varchar,
            raw_id uuid NOT NULL, published_version bigint NOT NULL,
            PRIMARY KEY(tenant_id,advertiser_id,ad_remote_id,platform_material_id,material_type))""",
        """CREATE TABLE material_account_member_current (
            tenant_id uuid NOT NULL, advertiser_id varchar(128) NOT NULL,
            platform_material_id varchar(128) NOT NULL, version_id uuid NOT NULL,
            use_count bigint NOT NULL CHECK(use_count>0), name varchar, ad_ids text[] NOT NULL,
            PRIMARY KEY(tenant_id,advertiser_id,platform_material_id))""",
        """CREATE FUNCTION qrm_retire_material_member() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            UPDATE report_evidence_version SET retired_at=COALESCE(retired_at,clock_timestamp()) WHERE id=OLD.version_id;
            RETURN OLD;
        END $$""",
    ]
    for table in ("material_ad_member_current", "material_account_member_current"):
        statements.append(
            f"CREATE TRIGGER retire_{table} AFTER DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION qrm_retire_material_member()"
        )
    statements.append("""CREATE FUNCTION qrm_refresh_material_members(p_tenant uuid,p_adv text,p_vid text)
    RETURNS void LANGUAGE plpgsql AS $$
    DECLARE source record; previous record; account_head record; next_version uuid;
        changed boolean := false; removed bigint; total bigint; display_name text;
        next_refs jsonb; next_uses jsonb; next_ads text[]; ref jsonb;
    BEGIN
        -- 同一个VID的不同广告共享账户聚合版本；锁顺序由statement调用者稳定排序。
        PERFORM pg_advisory_xact_lock(hashtextextended('material-member:'||p_tenant::text||':'||p_adv||':'||p_vid,0));
        IF NOT EXISTS(SELECT 1 FROM advertiser_account WHERE tenant_id=p_tenant AND advertiser_id=p_adv) THEN
            DELETE FROM material_ad_member_current WHERE tenant_id=p_tenant AND advertiser_id=p_adv AND platform_material_id=p_vid;
            DELETE FROM material_account_member_current WHERE tenant_id=p_tenant AND advertiser_id=p_adv AND platform_material_id=p_vid;
            RETURN;
        END IF;
        -- 当前表已执行完整/最新/精确优先规则；同版本多候选必须归并为NULL精确身份。
        -- raw_id最小候选决定名称，不能使用min(name)代替原DISTINCT ON语义。
        FOR source IN EXECUTE $source$
            SELECT DISTINCT ON (u.ad_remote_id,u.material_type)
                u.ad_remote_id,u.material_type,u.id AS raw_id,NULLIF(u.name,'') AS name,u.published_version,
                CASE WHEN count(*) OVER (PARTITION BY u.ad_remote_id,u.material_type)>1
                    THEN NULL ELSE u.ad_material_id END AS ad_material_id
            FROM current_ad_material u WHERE u.tenant_id=$1 AND u.advertiser_id=$2
                AND u.platform_material_id=$3 AND u.material_type='VIDEO'
            ORDER BY u.ad_remote_id,u.material_type,u.id
        $source$ USING p_tenant,p_adv,p_vid LOOP
            SELECT * INTO previous FROM material_ad_member_current
                WHERE tenant_id=p_tenant AND advertiser_id=p_adv AND platform_material_id=p_vid
                AND ad_remote_id=source.ad_remote_id AND material_type=source.material_type;
            IF FOUND AND previous.ad_material_id IS NOT DISTINCT FROM source.ad_material_id THEN
                -- 发布代数、展示名与选中原始行不改变行动身份，不创建或重写JSON证据。
                UPDATE material_ad_member_current SET name=source.name,raw_id=source.raw_id,published_version=source.published_version
                WHERE tenant_id=p_tenant AND advertiser_id=p_adv AND platform_material_id=p_vid
                    AND ad_remote_id=source.ad_remote_id AND material_type=source.material_type
                    AND (name,raw_id,published_version) IS DISTINCT FROM (source.name,source.raw_id,source.published_version);
                CONTINUE;
            END IF;
            changed := true;
            next_version := gen_random_uuid();
            ref := jsonb_build_object('tenant_id',p_tenant::text,'advertiser_id',p_adv,'kind','ad','remote_id',source.ad_remote_id);
            INSERT INTO report_evidence_version(id,tenant_id,advertiser_id,kind,refs,material_uses,naming_versions,created_at)
            VALUES(next_version,p_tenant,p_adv,'material_ad',jsonb_build_array(ref),jsonb_build_array(jsonb_build_object(
                'ad_ref',ref,'platform_material_id',p_vid,'ad_material_id',source.ad_material_id,'material_type',source.material_type)),
                '{}'::jsonb,clock_timestamp());
            INSERT INTO material_ad_member_current(tenant_id,advertiser_id,ad_remote_id,platform_material_id,material_type,
                version_id,ad_material_id,name,raw_id,published_version)
            VALUES(p_tenant,p_adv,source.ad_remote_id,p_vid,source.material_type,next_version,
                source.ad_material_id,source.name,source.raw_id,source.published_version)
            ON CONFLICT(tenant_id,advertiser_id,ad_remote_id,platform_material_id,material_type) DO UPDATE SET
                version_id=EXCLUDED.version_id,ad_material_id=EXCLUDED.ad_material_id,name=EXCLUDED.name,
                raw_id=EXCLUDED.raw_id,published_version=EXCLUDED.published_version;
            IF previous.version_id IS NOT NULL THEN
                UPDATE report_evidence_version SET retired_at=clock_timestamp() WHERE id=previous.version_id;
            END IF;
        END LOOP;
        DELETE FROM material_ad_member_current h WHERE h.tenant_id=p_tenant AND h.advertiser_id=p_adv AND h.platform_material_id=p_vid
            AND NOT EXISTS(SELECT 1 FROM current_ad_material u WHERE u.tenant_id=h.tenant_id AND u.advertiser_id=h.advertiser_id
                AND u.platform_material_id=h.platform_material_id AND u.ad_remote_id=h.ad_remote_id AND u.material_type=h.material_type);
        GET DIAGNOSTICS removed=ROW_COUNT;
        changed := changed OR removed>0;
        SELECT count(*),min(name COLLATE "C") INTO total,display_name FROM material_ad_member_current
            WHERE tenant_id=p_tenant AND advertiser_id=p_adv AND platform_material_id=p_vid;
        IF total=0 THEN
            DELETE FROM material_account_member_current WHERE tenant_id=p_tenant AND advertiser_id=p_adv AND platform_material_id=p_vid;
            RETURN;
        END IF;
        SELECT * INTO account_head FROM material_account_member_current
            WHERE tenant_id=p_tenant AND advertiser_id=p_adv AND platform_material_id=p_vid;
        IF FOUND AND NOT changed THEN
            UPDATE material_account_member_current SET name=display_name
                WHERE tenant_id=p_tenant AND advertiser_id=p_adv AND platform_material_id=p_vid AND name IS DISTINCT FROM display_name;
            RETURN;
        END IF;
        -- 仅成员身份变化时构建宽JSON；顺序与row_set原use_order严格一致。
        SELECT jsonb_agg(jsonb_build_object('tenant_id',p_tenant::text,'advertiser_id',p_adv,'kind','ad','remote_id',ad_remote_id)
                   ORDER BY ad_remote_id COLLATE "C"),array_agg(ad_remote_id ORDER BY ad_remote_id COLLATE "C")
            INTO next_refs,next_ads FROM (SELECT DISTINCT ad_remote_id FROM material_ad_member_current
                WHERE tenant_id=p_tenant AND advertiser_id=p_adv AND platform_material_id=p_vid) refs;
        SELECT jsonb_agg(jsonb_build_object('ad_ref',jsonb_build_object('tenant_id',p_tenant::text,'advertiser_id',p_adv,'kind','ad','remote_id',ad_remote_id),
                    'platform_material_id',platform_material_id,'ad_material_id',ad_material_id,'material_type',material_type)
                ORDER BY advertiser_id COLLATE "C",ad_remote_id COLLATE "C",platform_material_id COLLATE "C",COALESCE(ad_material_id,'') COLLATE "C",material_type COLLATE "C")
            INTO next_uses FROM material_ad_member_current WHERE tenant_id=p_tenant AND advertiser_id=p_adv AND platform_material_id=p_vid;
        next_version := gen_random_uuid();
        INSERT INTO report_evidence_version(id,tenant_id,advertiser_id,kind,refs,material_uses,naming_versions,created_at)
            VALUES(next_version,p_tenant,p_adv,'material_account',next_refs,next_uses,'{}'::jsonb,clock_timestamp());
        INSERT INTO material_account_member_current(tenant_id,advertiser_id,platform_material_id,version_id,use_count,name,ad_ids)
            VALUES(p_tenant,p_adv,p_vid,next_version,total,display_name,next_ads)
            ON CONFLICT(tenant_id,advertiser_id,platform_material_id) DO UPDATE SET
                version_id=EXCLUDED.version_id,use_count=EXCLUDED.use_count,name=EXCLUDED.name,ad_ids=EXCLUDED.ad_ids;
        IF account_head.version_id IS NOT NULL THEN
            UPDATE report_evidence_version SET retired_at=clock_timestamp() WHERE id=account_head.version_id;
        END IF;
    END $$""")
    for operation in ("INSERT", "UPDATE", "DELETE"):
        relations = (
            ("new_rows",)
            if operation == "INSERT"
            else ("old_rows",)
            if operation == "DELETE"
            else ("old_rows", "new_rows")
        )
        keys = " UNION ".join(
            f"SELECT DISTINCT tenant_id,advertiser_id,platform_material_id FROM {relation} WHERE material_type='VIDEO'"
            for relation in relations
        )
        present = " UNION ALL ".join(
            f"SELECT 1 FROM {relation}" for relation in relations
        )
        suffix = operation.lower()
        statements.append(f"""CREATE FUNCTION qrm_material_members_{suffix}() RETURNS trigger LANGUAGE plpgsql AS $$
            DECLARE k record;
            BEGIN
                IF NOT EXISTS({present}) THEN RETURN NULL; END IF;
                -- EXECUTE使空批次/小批次不会污染后续数万transition rows的计划。
                FOR k IN EXECUTE $keys$ SELECT * FROM ({keys}) touched
                    ORDER BY tenant_id,advertiser_id COLLATE "C",platform_material_id COLLATE "C" $keys$
                LOOP PERFORM qrm_refresh_material_members(k.tenant_id,k.advertiser_id,k.platform_material_id); END LOOP;
                RETURN NULL;
            END $$""")
        referencing = " ".join(
            ("OLD TABLE AS old_rows" if r == "old_rows" else "NEW TABLE AS new_rows")
            for r in relations
        )
        statements.append(f"""CREATE TRIGGER zz_material_members_{suffix} AFTER {operation} ON ad_material_reference
            REFERENCING {referencing} FOR EACH STATEMENT EXECUTE FUNCTION qrm_material_members_{suffix}()""")
    statements += [
        """CREATE FUNCTION qrm_material_members_account_insert() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE k record;
        BEGIN
            FOR k IN EXECUTE $keys$ SELECT DISTINCT u.tenant_id,u.advertiser_id,u.platform_material_id
                FROM new_accounts a JOIN current_ad_material u ON u.tenant_id=a.tenant_id AND u.advertiser_id=a.advertiser_id
                WHERE u.material_type='VIDEO' ORDER BY u.tenant_id,u.advertiser_id,u.platform_material_id $keys$
            LOOP PERFORM qrm_refresh_material_members(k.tenant_id,k.advertiser_id,k.platform_material_id); END LOOP;
            RETURN NULL;
        END $$""",
        """CREATE TRIGGER zz_material_members_account_insert AFTER INSERT ON advertiser_account
            REFERENCING NEW TABLE AS new_accounts FOR EACH STATEMENT EXECUTE FUNCTION qrm_material_members_account_insert()""",
    ]
    # 冻结源的bootstrap不复用运行时锁路径。先填新表，再用集合校验加回最终硬FK，
    # 避免新heads每行产生两次FK探测和巨型after-trigger队列；对外可见前全部验证。
    statements.extend(material_bootstrap_statements())
    for table, prefix in (
        ("material_ad_member_current", "fk_material_ad"),
        ("material_account_member_current", "fk_material_account"),
    ):
        statements.extend(
            [
                f"ALTER TABLE {table} ADD CONSTRAINT {prefix}_account FOREIGN KEY(tenant_id,advertiser_id) REFERENCES advertiser_account(tenant_id,advertiser_id) ON DELETE CASCADE NOT VALID",
                f"ALTER TABLE {table} ADD CONSTRAINT {prefix}_version FOREIGN KEY(tenant_id,advertiser_id,version_id) REFERENCES report_evidence_version(tenant_id,advertiser_id,id) NOT VALID",
                f"ALTER TABLE {table} VALIDATE CONSTRAINT {prefix}_account",
                f"ALTER TABLE {table} VALIDATE CONSTRAINT {prefix}_version",
            ]
        )
    statements.extend(
        [
            "CREATE INDEX ix_material_ad_member_video ON material_ad_member_current(tenant_id,platform_material_id,advertiser_id)",
            "CREATE INDEX ix_material_ad_member_ad ON material_ad_member_current(tenant_id,advertiser_id,ad_remote_id)",
            "CREATE INDEX ix_material_ad_member_version ON material_ad_member_current(version_id)",
            "CREATE INDEX ix_material_account_member_video ON material_account_member_current(tenant_id,platform_material_id,advertiser_id)",
            "CREATE INDEX ix_material_account_member_version ON material_account_member_current(version_id)",
        ]
    )
    return statements


def material_bootstrap_statements(*, batch_rows: int = 20_000) -> list[str]:
    """仅用于源写入已冻结、目标heads为空的首次迁移。

    只扫描一次current投影的VIDEO行，临时stage保存归并后身份与一次生成的UUID。
    按VID整组划批；单批最多batch_rows加最大单VID的使用数，不拆散同VID聚合。
    最大单VID本身就是不可拆分JSON证据，发布预检仍须核对它的行数/载荷容量。
    全局窄stage按账户/VID索引；仅批次临时表反复TRUNCATE，不积累advisory锁。
    全局临时空间与归并VIDEO数成正比，发布预检必须另计排序及临时文件容量。
    """
    if type(batch_rows) is not int or batch_rows <= 0:
        raise ValueError("bootstrap batch_rows must be a positive integer")
    return [
        """CREATE TEMP TABLE qrm_material_bootstrap_ad (
            tenant_id uuid NOT NULL,advertiser_id varchar(128) NOT NULL,
            ad_remote_id varchar(128) NOT NULL,platform_material_id varchar(128) NOT NULL,
            material_type varchar(64) NOT NULL,version_id uuid NOT NULL,
            ad_material_id varchar(128),name varchar,raw_id uuid NOT NULL,published_version bigint NOT NULL
        ) ON COMMIT DROP""",
        """CREATE TEMP TABLE qrm_material_bootstrap_groups (
            platform_material_id varchar(128) PRIMARY KEY,use_count bigint NOT NULL,batch_no bigint NOT NULL
        ) ON COMMIT DROP""",
        """CREATE INDEX qrm_material_bootstrap_groups_batch ON qrm_material_bootstrap_groups(batch_no)""",
        """CREATE TEMP TABLE qrm_material_bootstrap_account (
            tenant_id uuid NOT NULL,advertiser_id varchar(128) NOT NULL,
            platform_material_id varchar(128) NOT NULL,version_id uuid NOT NULL,
            use_count bigint NOT NULL,name varchar,ad_ids text[] NOT NULL,
            refs jsonb NOT NULL,material_uses jsonb NOT NULL
        ) ON COMMIT DROP""",
        f"""DO $material_bootstrap$
        DECLARE account_scope record; part record;
        BEGIN
            IF EXISTS(SELECT 1 FROM material_ad_member_current)
                OR EXISTS(SELECT 1 FROM material_account_member_current) THEN
                RAISE EXCEPTION 'material bootstrap requires empty target heads';
            END IF;
            -- 全源只扫描一次，且只保留VIDEO；不能对上万账户反复扫描宽current表。
            -- 完整/最新/精确优先已由current维护。窗口计数发生在DISTINCT ON之前。
            INSERT INTO qrm_material_bootstrap_ad
            SELECT chosen.tenant_id,chosen.advertiser_id,chosen.ad_remote_id,chosen.platform_material_id,
                chosen.material_type,gen_random_uuid(),chosen.ad_material_id,chosen.name,chosen.raw_id,chosen.published_version
            FROM (
                SELECT DISTINCT ON(u.tenant_id,u.advertiser_id,u.ad_remote_id,u.platform_material_id,u.material_type)
                    u.tenant_id,u.advertiser_id,u.ad_remote_id,u.platform_material_id,u.material_type,u.id AS raw_id,
                    NULLIF(u.name,'') AS name,u.published_version,
                    CASE WHEN count(*) OVER(PARTITION BY u.tenant_id,u.advertiser_id,u.ad_remote_id,u.platform_material_id,u.material_type)>1
                        THEN NULL ELSE u.ad_material_id END AS ad_material_id
                FROM current_ad_material u JOIN advertiser_account a
                    ON a.tenant_id=u.tenant_id AND a.advertiser_id=u.advertiser_id
                WHERE u.material_type='VIDEO'
                ORDER BY u.tenant_id,u.advertiser_id,u.ad_remote_id,u.platform_material_id,u.material_type,u.id
            ) chosen;
            -- 数千万VIDEO先顺序写临时heap，再bulk建索引，避免逐行维护随机B-tree。
            CREATE INDEX qrm_material_bootstrap_ad_vid ON qrm_material_bootstrap_ad(tenant_id,advertiser_id,platform_material_id);
            ANALYZE qrm_material_bootstrap_ad;
            FOR account_scope IN
                SELECT DISTINCT tenant_id,advertiser_id FROM qrm_material_bootstrap_ad
                ORDER BY tenant_id,advertiser_id
            LOOP
                TRUNCATE qrm_material_bootstrap_groups;
                EXECUTE $groups$
                    INSERT INTO qrm_material_bootstrap_groups
                    SELECT platform_material_id,use_count,
                        floor((sum(use_count) OVER(ORDER BY platform_material_id COLLATE "C")-use_count)/{batch_rows})::bigint
                    FROM (SELECT platform_material_id,count(*) AS use_count
                        FROM qrm_material_bootstrap_ad WHERE tenant_id=$1 AND advertiser_id=$2
                        GROUP BY platform_material_id) groups
                $groups$ USING account_scope.tenant_id,account_scope.advertiser_id;
                ANALYZE qrm_material_bootstrap_groups;
                FOR part IN SELECT DISTINCT batch_no FROM qrm_material_bootstrap_groups ORDER BY batch_no LOOP
                    -- 每条SPI写入有界，避免整库一次INSERT积压数千万FK after-trigger事件。
                    -- 临时UUID复用于两个INSERT，不能分别调用gen_random_uuid破坏硬引用。
                    EXECUTE $ad_evidence$
                        INSERT INTO report_evidence_version
                            (id,tenant_id,advertiser_id,kind,refs,material_uses,naming_versions,created_at)
                        SELECT u.version_id,u.tenant_id,u.advertiser_id,'material_ad',
                            jsonb_build_array(jsonb_build_object('tenant_id',u.tenant_id::text,'advertiser_id',u.advertiser_id,
                                'kind','ad','remote_id',u.ad_remote_id)),
                            jsonb_build_array(jsonb_build_object('ad_ref',jsonb_build_object('tenant_id',u.tenant_id::text,
                                'advertiser_id',u.advertiser_id,'kind','ad','remote_id',u.ad_remote_id),
                                'platform_material_id',u.platform_material_id,'ad_material_id',u.ad_material_id,'material_type',u.material_type)),
                            '{{}}'::jsonb,clock_timestamp()
                        FROM qrm_material_bootstrap_groups g JOIN qrm_material_bootstrap_ad u USING(platform_material_id)
                        WHERE g.batch_no=$1 AND u.tenant_id=$2 AND u.advertiser_id=$3
                    $ad_evidence$ USING part.batch_no,account_scope.tenant_id,account_scope.advertiser_id;
                    EXECUTE $ad_heads$
                        INSERT INTO material_ad_member_current(tenant_id,advertiser_id,ad_remote_id,platform_material_id,
                            material_type,version_id,ad_material_id,name,raw_id,published_version)
                        SELECT u.tenant_id,u.advertiser_id,u.ad_remote_id,u.platform_material_id,u.material_type,
                            u.version_id,u.ad_material_id,u.name,u.raw_id,u.published_version
                        FROM qrm_material_bootstrap_groups g JOIN qrm_material_bootstrap_ad u USING(platform_material_id)
                        WHERE g.batch_no=$1 AND u.tenant_id=$2 AND u.advertiser_id=$3
                    $ad_heads$ USING part.batch_no,account_scope.tenant_id,account_scope.advertiser_id;
                    TRUNCATE qrm_material_bootstrap_account;
                    -- VIDEO归并后同账户/VID/ad只有一行，故refs与ad_ids自然按广告去重。
                    EXECUTE $account_stage$
                        INSERT INTO qrm_material_bootstrap_account
                        SELECT u.tenant_id,u.advertiser_id,u.platform_material_id,gen_random_uuid(),count(*),
                            min(u.name COLLATE "C"),array_agg(u.ad_remote_id ORDER BY u.ad_remote_id COLLATE "C"),
                            jsonb_agg(jsonb_build_object('tenant_id',u.tenant_id::text,'advertiser_id',u.advertiser_id,
                                'kind','ad','remote_id',u.ad_remote_id) ORDER BY u.ad_remote_id COLLATE "C"),
                            jsonb_agg(jsonb_build_object('ad_ref',jsonb_build_object('tenant_id',u.tenant_id::text,
                                'advertiser_id',u.advertiser_id,'kind','ad','remote_id',u.ad_remote_id),
                                'platform_material_id',u.platform_material_id,'ad_material_id',u.ad_material_id,'material_type',u.material_type)
                                ORDER BY u.advertiser_id COLLATE "C",u.ad_remote_id COLLATE "C",u.platform_material_id COLLATE "C",
                                    COALESCE(u.ad_material_id,'') COLLATE "C",u.material_type COLLATE "C")
                        FROM qrm_material_bootstrap_groups g JOIN qrm_material_bootstrap_ad u USING(platform_material_id)
                        WHERE g.batch_no=$1 AND u.tenant_id=$2 AND u.advertiser_id=$3 GROUP BY u.tenant_id,u.advertiser_id,u.platform_material_id
                    $account_stage$ USING part.batch_no,account_scope.tenant_id,account_scope.advertiser_id;
                    INSERT INTO report_evidence_version
                        (id,tenant_id,advertiser_id,kind,refs,material_uses,naming_versions,created_at)
                    SELECT version_id,tenant_id,advertiser_id,'material_account',refs,material_uses,'{{}}'::jsonb,clock_timestamp()
                        FROM qrm_material_bootstrap_account;
                    INSERT INTO material_account_member_current
                        (tenant_id,advertiser_id,platform_material_id,version_id,use_count,name,ad_ids)
                    SELECT tenant_id,advertiser_id,platform_material_id,version_id,use_count,name,ad_ids
                        FROM qrm_material_bootstrap_account;
                END LOOP;
            END LOOP;
        END $material_bootstrap$""",
        "DROP TABLE qrm_material_bootstrap_account,qrm_material_bootstrap_groups,qrm_material_bootstrap_ad",
    ]
