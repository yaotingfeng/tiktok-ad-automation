"""Exercise pg_dump/pg_restore on disposable synthetic databases, without workers.

Requires an explicitly named *_test DATABASE_URL and matching PostgreSQL clients.
Only temporary databases created by this process are restored or dropped. The
archive is deleted on exit; the optional JSON contains counts and timing only.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest.mock import patch
from uuid import uuid4

from cryptography.fernet import Fernet
from sqlalchemy import inspect, text
from sqlalchemy.engine import URL, Engine
from sqlmodel import Session

from app.core.config import settings
from app.core.credentials import decrypt_credentials, encrypt_credentials
from app.jobs.models import PendingDispatch
from app.models import User
from app.modules.accounts.models import TenantBC, TikTokConnection
from app.modules.materials.models import MaterialFile
from app.modules.tenants.models import Tenant, TenantMembership
from scripts.benchmark_batches import owned_database, validate_database_url


def client_environment(url: URL) -> dict[str, str]:
    return {
        **os.environ,
        "PGHOST": url.host or "localhost",
        "PGPORT": str(url.port or 5432),
        "PGUSER": url.username or "postgres",
        "PGPASSWORD": url.password or "",
        "PGDATABASE": url.database or "",
        "PGCONNECT_TIMEOUT": "10",
    }


def fingerprint(engine: Engine) -> dict[str, object]:
    # Names come from the DB inspector and are quoted, never from user input.
    quote = engine.dialect.identifier_preparer.quote
    with engine.connect() as connection:
        return {
            "migration": connection.scalar(
                text("SELECT version_num FROM alembic_version")
            ),
            "rows": {
                name: connection.scalar(text(f"SELECT count(*) FROM {quote(name)}"))
                for name in sorted(inspect(connection).get_table_names())
            },
        }


def rehearse(*, output: Path | None = None) -> dict[str, object]:
    base = validate_database_url(os.environ.get("DATABASE_URL", ""))
    for name in ("pg_dump", "pg_restore"):
        if shutil.which(name) is None:
            raise RuntimeError(f"Install a matching PostgreSQL client: {name}")
    started = perf_counter()
    with (
        TemporaryDirectory(prefix="tiktok-restore-") as temporary,
        patch.object(
            settings, "CONNECTION_ENCRYPTION_KEY", Fernet.generate_key().decode()
        ),
        owned_database(base.render_as_string(hide_password=False)) as source,
        owned_database(base.render_as_string(hide_password=False)) as target,
    ):
        with Session(source) as session, session.begin():
            tenant = Tenant(name="Synthetic restore rehearsal", active=False)
            actor = User(email=f"{uuid4()}@example.com", hashed_password="unused")
            session.add_all([tenant, actor])
            session.flush()
            session.add(TenantBC(tenant_id=tenant.id, bc_id="synthetic-restore-bc"))
            session.flush()
            connection = TikTokConnection(
                tenant_id=tenant.id,
                status="ACTIVE",
                credential_version=1,
                credential_ciphertext=encrypt_credentials(
                    tenant_id=tenant.id,
                    value={"access_token": "synthetic-restore-token"},
                ),
            )
            material = MaterialFile(
                tenant_id=tenant.id,
                bc_id="synthetic-restore-bc",
                file_name="The Bond - restore.mp4",
                object_key="synthetic/restore/original.mp4",
                byte_size=1024,
                sha256="a" * 64,
                video_md5="b" * 32,
                storage_state="stored",
            )
            dispatch = PendingDispatch(
                tenant_id=tenant.id,
                actor_id=actor.id,
                task_name="jobs.probe",
                task_key="synthetic-restore-no-consumer",
                payload={"synthetic": True},
                attempts=2,
            )
            session.add_all(
                [
                    TenantMembership(
                        tenant_id=tenant.id, user_id=actor.id, role="tenant_admin"
                    ),
                    connection,
                    material,
                    dispatch,
                ]
            )
            session.flush()
            tenant_id, connection_id = tenant.id, connection.id
            material_id, dispatch_id = material.id, dispatch.id
        before = fingerprint(source)
        archive = Path(temporary) / "synthetic.dump"
        subprocess.run(
            [
                "pg_dump",
                "--format=custom",
                "--no-owner",
                "--no-acl",
                "--file",
                str(archive),
            ],
            env=client_environment(source.url),
            check=True,
        )
        subprocess.run(
            [
                "pg_restore",
                "--clean",
                "--if-exists",
                "--no-owner",
                "--no-acl",
                "--exit-on-error",
                "--dbname",
                target.url.database or "",
                str(archive),
            ],
            env=client_environment(target.url),
            check=True,
        )
        after = fingerprint(target)
        assert before == after, "Migration head or row counts changed during restore"
        with Session(target) as session:
            saved = session.get(TikTokConnection, connection_id)
            assert saved and saved.credential_ciphertext
            assert decrypt_credentials(
                tenant_id=tenant_id, ciphertext=saved.credential_ciphertext
            ) == {"access_token": "synthetic-restore-token"}
            stored = session.get(MaterialFile, material_id)
            assert stored and stored.object_key == "synthetic/restore/original.mp4"
            pending = session.get(PendingDispatch, dispatch_id)
            assert pending and pending.attempts == 2 and pending.published_at is None
            restored_tenant = session.get(Tenant, tenant_id)
            assert restored_tenant and restored_tenant.active is False
        result = {
            "complete": True,
            "seconds": round(perf_counter() - started, 3),
            "schema": before,
            "archive_bytes": archive.stat().st_size,
            "credential_decryption": "passed with original synthetic key",
            "material_object_metadata": "preserved; object storage itself was not exercised",
            "pending_dispatch": "preserved without starting any consumer",
            "external_calls": 0,
            "scope": "two disposable synthetic databases; not a business backup",
        }
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    sys.stdout.write(json.dumps(rehearse(output=arguments.output), indent=2) + "\n")
