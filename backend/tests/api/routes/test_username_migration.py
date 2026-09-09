"""Real PostgreSQL backup/restore and upgrade of existing user identities."""

import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import make_url
from sqlmodel import Session, create_engine, select

from app.core.config import settings
from app.core.security import get_password_hash, verify_password
from app.models import User
from app.modules.tenants.service import require_tenant
from tests.database import require_test_database
from tests.pg_backup import backup_tools, run_backup_tool


def test_backup_restore_upgrade_preserves_identity_and_resolves_collisions(
    monkeypatch, tmp_path
):
    require_test_database(str(settings.DATABASE_URL))
    base = make_url(str(settings.DATABASE_URL))
    admin_url = base.set(drivername="postgresql", database="postgres")
    names = [f"username_{uuid4().hex}_test" for _ in range(2)]
    created = []
    engines = []
    backend = Path(__file__).resolve().parents[3]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "app/alembic"))
    try:
        with psycopg.connect(
            admin_url.render_as_string(hide_password=False), autocommit=True
        ) as admin:
            dump_tool, restore_tool = backup_tools(admin.info.server_version // 10000)
            for name in names:
                admin.execute(
                    sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name))
                )
                created.append(name)
        urls = [base.set(database=name) for name in names]
        engines = [create_engine(url) for url in urls]
        with monkeypatch.context() as patch:
            patch.setattr(
                settings, "DATABASE_URL", urls[0].render_as_string(hide_password=False)
            )
            command.upgrade(config, "0014_recovery_candidates")
        emails = [
            "ADMIN@example.com",
            "Admin@another.invalid",
            "张三@example.invalid",
            "name+tag@example.invalid",
            "name-tag@example.invalid",
            "TEAM@example.invalid",
            "team@other.invalid",
            "L" * 120 + "@example.invalid",
            "admin@example.com",
        ]
        hashed = get_password_hash("Migration-only-password-123")
        now = datetime(2026, 1, 1, tzinfo=UTC)
        admin_id = UUID(int=9)
        tenant_id = uuid4()
        with psycopg.connect(
            urls[0].set(drivername="postgresql").render_as_string(hide_password=False)
        ) as db:
            for index, email in enumerate(emails, 1):
                db.execute(
                    'INSERT INTO "user" (id,email,hashed_password,full_name,is_active,is_superuser,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)',
                    (
                        UUID(int=index),
                        email,
                        hashed,
                        f"Member {index}",
                        index != 7,
                        index == 9,
                        now,
                    ),
                )
            db.execute(
                "INSERT INTO tenant (id,name,active) VALUES (%s,%s,true)",
                (tenant_id, "Preserved tenant"),
            )
            db.execute(
                "INSERT INTO tenant_membership (tenant_id,user_id,role,active) VALUES (%s,%s,%s,true)",
                (tenant_id, admin_id, "tenant_admin"),
            )
            db.execute(
                "INSERT INTO tenant_membership (tenant_id,user_id,role,active) VALUES (%s,%s,%s,true)",
                (tenant_id, UUID(int=1), "operator"),
            )
            db.execute(
                "INSERT INTO audit_event (id,tenant_id,actor_id,action,target_id,details,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (
                    uuid4(),
                    tenant_id,
                    UUID(int=1),
                    "existing-action",
                    str(admin_id),
                    "{}",
                    now,
                ),
            )
        env = os.environ.copy()
        env.update(
            PGHOST=base.host or "localhost",
            PGPORT=str(base.port or 5432),
            PGUSER=base.username or "",
            PGPASSWORD=base.password or "",
            PGDATABASE=names[0],
        )
        backup = tmp_path / "private-pre-upgrade.dump"
        run_backup_tool(
            dump_tool, ["--format=custom", "--no-owner", "--file", str(backup)], env
        )
        backup.chmod(0o600)
        env["PGDATABASE"] = names[1]
        run_backup_tool(
            restore_tool,
            [
                "--no-owner",
                "--no-privileges",
                "--dbname",
                names[1],
                str(backup),
            ],
            env=env,
        )
        mappings = []
        for index, url in enumerate(urls):
            with monkeypatch.context() as patch:
                patch.setattr(
                    settings, "DATABASE_URL", url.render_as_string(hide_password=False)
                )
                command.upgrade(config, "head")
                command.check(config)
            with Session(engines[index]) as session:
                users = session.exec(select(User).order_by(User.id)).all()
                assert len(users) == len(emails)
                mapping = {user.id: user.username for user in users}
                mappings.append(mapping)
                assert mapping[admin_id] == "admin"
                assert len(set(mapping.values())) == len(emails)
                for i, user in enumerate(users, 1):
                    assert user.id == UUID(int=i)
                    assert user.hashed_password == hashed
                    assert verify_password(
                        "Migration-only-password-123", user.hashed_password
                    )[0]
                    assert user.created_at == now and user.full_name == f"Member {i}"
                    assert user.is_active == (i != 7) and user.is_superuser == (i == 9)
                assert (
                    require_tenant(
                        session, actor_id=admin_id, tenant_id=tenant_id, action="manage"
                    ).tenant_id
                    == tenant_id
                )
                assert (
                    require_tenant(
                        session,
                        actor_id=UUID(int=1),
                        tenant_id=tenant_id,
                        action="read",
                    ).role
                    == "operator"
                )
                members = (
                    session.connection()
                    .exec_driver_sql("SELECT user_id, role FROM tenant_membership")
                    .all()
                )
                assert set(members) == {
                    (admin_id, "tenant_admin"),
                    (UUID(int=1), "operator"),
                }
                assert session.connection().exec_driver_sql(
                    "SELECT actor_id FROM audit_event"
                ).scalar_one() == UUID(int=1)
                columns = (
                    session.connection()
                    .exec_driver_sql(
                        "SELECT column_name FROM information_schema.columns WHERE table_name='user'"
                    )
                    .scalars()
                    .all()
                )
                assert "email" not in columns and "username" in columns
        assert mappings[0] == mappings[1]
    finally:
        for engine in engines:
            engine.dispose()
        with psycopg.connect(
            admin_url.render_as_string(hide_password=False), autocommit=True
        ) as admin:
            for name in created:
                admin.execute(
                    sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                        sql.Identifier(name)
                    )
                )
