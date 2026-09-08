"""Fence directory changes with transactionally maintained scope revisions.

Statement transition tables deduplicate affected scopes before ordered UPSERTs.
Capability publication fields are deliberately absent from the compared tuples.
"""

import sqlalchemy as sa
import sqlmodel.sql.sqltypes
from alembic import op

revision = "0007_directory_revision"
down_revision = "0006a_unit_dispatch"
branch_labels = None
depends_on = None

_GRANT_FACTS = "tenant_id,bc_id,connection_id,advertiser_id,in_bc,authorized,active,last_seen_run_id"
_ACCOUNT_FACTS = "tenant_id,advertiser_id,currency,timezone,remote_status,ownership_conflict"


def _touch_function(name: str, scopes: str) -> None:
    # Sources below are fixed migration SQL, never application/user input.
    op.execute(f"""
        CREATE FUNCTION public.{name}() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $function$
        BEGIN
            INSERT INTO public.account_directory_revision AS fence
                (tenant_id,bc_id,connection_id,revision)
            SELECT touched.tenant_id,touched.bc_id,touched.connection_id,1
            FROM ({scopes}) AS touched
            JOIN public.tenant_bc AS bc
              ON bc.tenant_id=touched.tenant_id AND bc.bc_id=touched.bc_id
            JOIN public.tiktok_connection AS connection
              ON connection.tenant_id=touched.tenant_id AND connection.id=touched.connection_id
            ORDER BY touched.tenant_id,touched.bc_id COLLATE "C",touched.connection_id
            ON CONFLICT (tenant_id,bc_id,connection_id)
                DO UPDATE SET revision=fence.revision+1;
            RETURN NULL;
        END;
        $function$;
    """)


def upgrade() -> None:
    # Prevent a directory write from landing between backfill and trigger install.
    # Readers may continue; all DDL/backfill/trigger changes commit atomically.
    op.execute("LOCK TABLE public.advertiser_account, public.bc_account_access IN SHARE ROW EXCLUSIVE MODE")
    op.create_table(
        "account_directory_revision",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("bc_id", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "bc_id", "connection_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "bc_id"], ["tenant_bc.tenant_id", "tenant_bc.bc_id"],
            name="fk_directory_revision_bc", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "connection_id"], ["tiktok_connection.tenant_id", "tiktok_connection.id"],
            name="fk_directory_revision_connection", ondelete="CASCADE",
        ),
        sa.CheckConstraint("revision >= 0", name="ck_directory_revision_nonnegative"),
    )
    op.execute("""
        INSERT INTO public.account_directory_revision (tenant_id,bc_id,connection_id,revision)
        SELECT DISTINCT tenant_id,bc_id,connection_id,1 FROM public.bc_account_access
        ORDER BY tenant_id,bc_id,connection_id
    """)
    # Serialize association changes with metadata updates before either can
    # publish a revision based on a directory snapshot missing the other writer.
    # Advisory locks coordinate writers; persistent SQL revisions remain evidence.
    # Hash collisions only add serialization, never merge tenant/scope state.
    op.execute("""
        CREATE FUNCTION public.account_directory_lock_account() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $function$
        DECLARE account_key record;
        BEGIN
            FOR account_key IN
                SELECT DISTINCT tenant_id,advertiser_id COLLATE "C" AS advertiser_id FROM (
                    SELECT NEW.tenant_id,NEW.advertiser_id
                    UNION ALL SELECT OLD.tenant_id,OLD.advertiser_id
                ) AS keys ORDER BY tenant_id,advertiser_id COLLATE "C"
            LOOP
                PERFORM pg_advisory_xact_lock(hashtextextended(
                    jsonb_build_array(account_key.tenant_id,account_key.advertiser_id)::text,
                    700701));
            END LOOP;
            RETURN NEW;
        END;
        $function$;
    """)
    op.execute("""
        CREATE FUNCTION public.account_directory_lock_membership() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $function$
        DECLARE account_key record;
        BEGIN
            IF TG_OP = 'INSERT' THEN
                PERFORM pg_advisory_xact_lock(hashtextextended(
                    jsonb_build_array(NEW.tenant_id,NEW.advertiser_id)::text,700701));
            ELSE
                FOR account_key IN
                    SELECT DISTINCT tenant_id,advertiser_id COLLATE "C" AS advertiser_id FROM (
                        SELECT NEW.tenant_id,NEW.advertiser_id
                        UNION ALL SELECT OLD.tenant_id,OLD.advertiser_id
                    ) AS keys ORDER BY tenant_id,advertiser_id COLLATE "C"
                LOOP
                    PERFORM pg_advisory_xact_lock(hashtextextended(
                        jsonb_build_array(account_key.tenant_id,account_key.advertiser_id)::text,
                        700701));
                END LOOP;
            END IF;
            RETURN NEW;
        END;
        $function$;
    """)
    op.execute("""
        CREATE TRIGGER directory_account_lock BEFORE UPDATE ON public.advertiser_account
        FOR EACH ROW WHEN (
            ROW(OLD.tenant_id,OLD.advertiser_id,OLD.currency,OLD.timezone,
                OLD.remote_status,OLD.ownership_conflict) IS DISTINCT FROM
            ROW(NEW.tenant_id,NEW.advertiser_id,NEW.currency,NEW.timezone,
                NEW.remote_status,NEW.ownership_conflict))
        EXECUTE FUNCTION public.account_directory_lock_account()
    """)
    op.execute("""
        CREATE TRIGGER directory_membership_insert_lock BEFORE INSERT ON public.bc_account_access
        FOR EACH ROW EXECUTE FUNCTION public.account_directory_lock_membership()
    """)
    op.execute("""
        CREATE TRIGGER directory_membership_update_lock BEFORE UPDATE ON public.bc_account_access
        FOR EACH ROW WHEN (
            ROW(OLD.tenant_id,OLD.advertiser_id,OLD.bc_id,OLD.connection_id) IS DISTINCT FROM
            ROW(NEW.tenant_id,NEW.advertiser_id,NEW.bc_id,NEW.connection_id))
        EXECUTE FUNCTION public.account_directory_lock_membership()
    """)
    _touch_function(
        "account_directory_grant_insert",
        "SELECT DISTINCT tenant_id,bc_id,connection_id FROM new_rows",
    )
    _touch_function(
        "account_directory_grant_delete",
        "SELECT DISTINCT tenant_id,bc_id,connection_id FROM old_rows",
    )
    _touch_function(
        "account_directory_grant_update",
        f"""SELECT DISTINCT tenant_id,bc_id,connection_id FROM (
            (SELECT {_GRANT_FACTS} FROM new_rows EXCEPT SELECT {_GRANT_FACTS} FROM old_rows)
            UNION
            (SELECT {_GRANT_FACTS} FROM old_rows EXCEPT SELECT {_GRANT_FACTS} FROM new_rows)
        ) AS changed""",
    )
    _touch_function(
        "account_directory_account_update",
        f"""SELECT DISTINCT g.tenant_id,g.bc_id,g.connection_id
        FROM public.bc_account_access AS g JOIN (
            (SELECT {_ACCOUNT_FACTS} FROM new_rows EXCEPT SELECT {_ACCOUNT_FACTS} FROM old_rows)
            UNION
            (SELECT {_ACCOUNT_FACTS} FROM old_rows EXCEPT SELECT {_ACCOUNT_FACTS} FROM new_rows)
        ) AS changed
        ON g.tenant_id=changed.tenant_id AND g.advertiser_id=changed.advertiser_id""",
    )
    # Current account/grant foreign keys are restrictive; if a future migration
    # adds cascading membership deletes, the grant DELETE trigger still fences it.
    _touch_function(
        "account_directory_grant_truncate",
        "SELECT tenant_id,bc_id,connection_id FROM public.account_directory_revision",
    )
    for event, transition in (
        ("INSERT", "NEW TABLE AS new_rows"),
        ("DELETE", "OLD TABLE AS old_rows"),
        ("UPDATE", "OLD TABLE AS old_rows NEW TABLE AS new_rows"),
    ):
        op.execute(f"""
            CREATE TRIGGER directory_grant_{event.lower()}
            AFTER {event} ON public.bc_account_access
            REFERENCING {transition} FOR EACH STATEMENT
            EXECUTE FUNCTION public.account_directory_grant_{event.lower()}()
        """)
    op.execute("""
        CREATE TRIGGER directory_account_update AFTER UPDATE ON public.advertiser_account
        REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows FOR EACH STATEMENT
        EXECUTE FUNCTION public.account_directory_account_update()
    """)
    op.execute("""
        CREATE TRIGGER directory_grant_truncate AFTER TRUNCATE ON public.bc_account_access
        FOR EACH STATEMENT EXECUTE FUNCTION public.account_directory_grant_truncate()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER directory_account_lock ON public.advertiser_account")
    op.execute("DROP TRIGGER directory_membership_insert_lock ON public.bc_account_access")
    op.execute("DROP TRIGGER directory_membership_update_lock ON public.bc_account_access")
    op.execute("DROP FUNCTION public.account_directory_lock_account()")
    op.execute("DROP FUNCTION public.account_directory_lock_membership()")
    for event in ("insert", "delete", "update", "truncate"):
        op.execute(f"DROP TRIGGER directory_grant_{event} ON public.bc_account_access")
        op.execute(f"DROP FUNCTION public.account_directory_grant_{event}()")
    op.execute("DROP TRIGGER directory_account_update ON public.advertiser_account")
    op.execute("DROP FUNCTION public.account_directory_account_update()")
    op.drop_table("account_directory_revision")
