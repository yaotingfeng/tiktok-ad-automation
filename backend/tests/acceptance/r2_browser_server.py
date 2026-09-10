"""Owned-DB browser acceptance: real FastAPI/JWT, loopback-only storage double.

DATABASE_URL must already name a local test database. We create a different,
random database, record ownership, migrate only it and drop only it on exit.
No worker, broker, provider or TikTok connection is started.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import secrets
import signal
import ssl
import subprocess
from collections import Counter
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock, Thread
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import boto3
import psycopg
import uvicorn
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from psycopg import sql
from sqlalchemy.engine import make_url

APP_ORIGIN = "http://127.0.0.1:5293"
STORAGE_ORIGIN = "https://127.0.0.1:5294"
BACKEND = Path(__file__).resolve().parents[2]


class StorageWire:
    """Storage interface double. PUT bytes cross actual HTTPS, never business mocks."""

    def __init__(self):
        self.lock = RLock()
        self.uploads: dict[str, dict[str, Any]] = {}
        self.objects: dict[str, dict[str, Any]] = {}
        self.signatures: dict[str, dict[str, Any]] = {}
        self.puts: list[dict[str, Any]] = []
        self.credential_header_violations = 0
        # Real boto signing, exclusively synthetic credentials and loopback URL.
        self.signer = boto3.client(
            "s3",
            endpoint_url=STORAGE_ORIGIN,
            region_name="auto",
            aws_access_key_id="synthetic-browser-key",
            aws_secret_access_key="synthetic-browser-signing-secret",
            config=BotoConfig(
                signature_version="s3v4", s3={"addressing_style": "path"}
            ),
        )

    def close(self):
        # Production closes its per-call adapter; the fixture owns the signer.
        pass

    def create_multipart_upload(self, **values):
        assert "ACL" not in values
        with self.lock:
            identity = str(uuid4())
            self.uploads[identity] = {**values, "parts": {}}
            return {"UploadId": identity}

    def list_multipart_uploads(self, **values):
        with self.lock:
            return {
                "Uploads": [
                    {"Key": row["Key"], "UploadId": key}
                    for key, row in self.uploads.items()
                    if row["Key"].startswith(values["Prefix"])
                ],
                "IsTruncated": False,
            }

    def list_parts(self, **values):
        with self.lock:
            row = self.uploads[values["UploadId"]]
            assert row["Key"] == values["Key"]
            parts = [
                {"PartNumber": n, "ETag": value["etag"], "Size": len(value["body"])}
                for n, value in sorted(row["parts"].items())
                if n > values.get("PartNumberMarker", 0)
            ]
            selected = parts[: values["MaxParts"]]
            return {
                "Parts": selected,
                "IsTruncated": len(parts) > len(selected),
                **(
                    {"NextPartNumberMarker": selected[-1]["PartNumber"]}
                    if len(parts) > len(selected)
                    else {}
                ),
            }

    def generate_presigned_url(self, operation, **values):
        assert operation == "upload_part"
        params = values["Params"]
        assert 0 < params["ContentLength"] <= 16 * 1024**2
        with self.lock:
            row = self.uploads[params["UploadId"]]
            assert row["Key"] == params["Key"] and row["Bucket"] == params["Bucket"]
            url = self.signer.generate_presigned_url(operation, **values)
            query = parse_qs(urlsplit(url).query)
            assert "content-length" in query["X-Amz-SignedHeaders"][0]
            self.signatures[query["X-Amz-Signature"][0]] = {
                **params,
                "path": urlsplit(url).path,
                "expires": datetime.strptime(
                    query["X-Amz-Date"][0], "%Y%m%dT%H%M%SZ"
                ).replace(tzinfo=UTC)
                + timedelta(seconds=int(query["X-Amz-Expires"][0])),
            }
            return url

    def complete_multipart_upload(self, **values):
        with self.lock:
            row = self.uploads[values["UploadId"]]
            assert row["Key"] == values["Key"]
            expected = [
                {"PartNumber": n, "ETag": p["etag"]}
                for n, p in sorted(row["parts"].items())
            ]
            assert values["MultipartUpload"]["Parts"] == expected
            body = b"".join(p["body"] for _, p in sorted(row["parts"].items()))
            self.objects[values["Key"]] = {
                "ContentLength": len(body),
                "ContentType": row["ContentType"],
                "Metadata": row["Metadata"],
                "body": body,
            }
            return {"ETag": '"synthetic-multipart-etag"'}

    def head_object(self, **values):
        with self.lock:
            if values["Key"] not in self.objects:
                raise ClientError(
                    {
                        "Error": {"Code": "404"},
                        "ResponseMetadata": {"HTTPStatusCode": 404},
                    },
                    "HeadObject",
                )
            return {
                key: value
                for key, value in self.objects[values["Key"]].items()
                if key != "body"
            }


def storage_server(remote: StorageWire, directory: Path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "loopback-r2-test")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any):
            pass  # URLs contain ephemeral synthetic signatures.

        def reply(self, status, etag=None):
            self.send_response(status)
            self.send_header("Access-Control-Allow-Origin", APP_ORIGIN)
            self.send_header("Access-Control-Allow-Methods", "PUT, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "content-type")
            self.send_header("Access-Control-Expose-Headers", "ETag")
            self.send_header("Content-Length", "0")
            if etag:
                self.send_header("ETag", etag)
            self.end_headers()

        def do_OPTIONS(self):
            self.reply(204 if self.headers.get("Origin") == APP_ORIGIN else 403)

        def do_PUT(self):
            query = parse_qs(urlsplit(self.path).query)
            signature = query.get("X-Amz-Signature", [""])[0]
            with remote.lock:
                permission = remote.signatures.get(signature)
                if any(
                    self.headers.get(name)
                    for name in ("Authorization", "Cookie", "Referer")
                ):
                    remote.credential_header_violations += 1
                    return self.reply(403)
                if (
                    not permission
                    or self.headers.get("Origin") != APP_ORIGIN
                    or permission["expires"] <= datetime.now(UTC)
                ):
                    return self.reply(403)
                if (
                    urlsplit(self.path).path != permission["path"]
                    or query.get("uploadId") != [permission["UploadId"]]
                    or query.get("partNumber") != [str(permission["PartNumber"])]
                    or self.headers.get("Content-Length")
                    != str(permission["ContentLength"])
                ):
                    return self.reply(400)
            body = self.rfile.read(permission["ContentLength"])
            if len(body) != permission["ContentLength"]:
                return self.reply(400)
            etag = '"' + hashlib.md5(body).hexdigest() + '"'
            with remote.lock:
                remote.uploads[permission["UploadId"]]["parts"][
                    permission["PartNumber"]
                ] = {"etag": etag, "body": body}
                remote.puts.append(
                    {
                        "key": permission["Key"],
                        "size": len(body),
                        "sha256": hashlib.sha256(body).hexdigest(),
                    }
                )
            self.reply(200, etag)

    server = ThreadingHTTPServer(("127.0.0.1", 5294), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def create_app(remote: StorageWire, video: bytes):
    from alembic import command
    from alembic.config import Config
    from fastapi import HTTPException, Response
    from sqlalchemy import func
    from sqlmodel import Session, col, select

    from app.core.db import engine
    from app.core.security import get_password_hash
    from app.jobs.models import PendingDispatch
    from app.main import FRONTEND_DIR, app
    from app.models import User
    from app.modules.accounts.models import TenantBC
    from app.modules.materials import storage
    from app.modules.materials.ingest_models import (
        IngestSession,
        OriginalUse,
        TemporaryMaterialObject,
    )
    from app.modules.materials.models import AccountMaterial, MaterialFile
    from app.modules.tenants.models import Tenant, TenantMembership

    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "app/alembic"))
    command.upgrade(config, "head")
    if not (FRONTEND_DIR / "index.html").is_file():
        raise RuntimeError("Build the frontend before browser acceptance")

    def synthetic_storage(obj: Any) -> Any:
        assert obj.storage_provider == "r2"
        return remote

    patch.object(storage, "make_object_s3", synthetic_storage).start()
    scopes = set()
    frontend_routes = [
        route
        for route in app.router.routes
        if getattr(route, "path", None) in {"", "/{path:path}"}
    ]
    for route in frontend_routes:
        app.router.routes.remove(route)

    @app.get("/__r2_acceptance__/health")
    def health():
        return {"ready": True}

    @app.get("/__r2_acceptance__/video")
    def clip():
        return Response(
            video, media_type="video/mp4", headers={"Cache-Control": "no-store"}
        )

    @app.post("/__r2_acceptance__/scenario")
    def seed():
        suffix = uuid4().hex[:12]
        password = "Synthetic-" + secrets.token_urlsafe(16)
        with Session(engine) as db, db.begin():
            tenant, other = (
                Tenant(name="R2 浏览器验收 " + suffix),
                Tenant(name="隔离租户 " + suffix),
            )
            user = User(
                username="r2_" + suffix,
                hashed_password=get_password_hash(password),
                full_name="浏览器验收投手",
            )
            db.add_all([tenant, other, user])
            db.flush()
            bc = str(10**19 + int(suffix, 16))
            db.add(
                TenantMembership(tenant_id=tenant.id, user_id=user.id, role="operator")
            )
            db.add(TenantBC(tenant_id=tenant.id, bc_id=bc, name="合成素材 BC"))
            result = {
                "tenant_id": str(tenant.id),
                "bc_id": bc,
                "username": user.username,
                "password": password,
                "other_tenant_id": str(other.id),
            }
            scopes.add(tenant.id)
            return result

    @app.get("/__r2_acceptance__/evidence/{tenant_id}")
    def evidence(tenant_id: UUID):
        if tenant_id not in scopes:
            raise HTTPException(404)
        with Session(engine) as db:

            def count(model, *conditions):
                return db.exec(
                    select(func.count())
                    .select_from(model)
                    .where(model.tenant_id == tenant_id, *conditions)
                ).one()

            outcomes = db.exec(
                select(OriginalUse.completion_evidence).where(
                    OriginalUse.tenant_id == tenant_id,
                    OriginalUse.purpose == "part_put",
                )
            ).all()
            result = {
                "session_count": count(IngestSession),
                "material_count": count(MaterialFile),
                "stored_count": count(
                    TemporaryMaterialObject, TemporaryMaterialObject.status == "stored"
                ),
                "validator_dispatch_count": count(
                    PendingDispatch,
                    PendingDispatch.task_name == "materials.validate_original",
                    col(PendingDispatch.published_at).is_(None),
                ),
                "available_asset_count": count(AccountMaterial),
                "permission_outcomes": dict(
                    Counter(row.get("outcome") for row in outcomes)
                ),
            }
        prefix = f"tenants/{tenant_id}/"
        with remote.lock:
            received = [item for item in remote.puts if item["key"].startswith(prefix)]
            result.update(
                {
                    "received_bytes": sum(item["size"] for item in received),
                    "successful_puts": len(received),
                    "completed_objects": sum(
                        key.startswith(prefix) for key in remote.objects
                    ),
                    "credential_header_violations": remote.credential_header_violations,
                    "object_sha256": [
                        hashlib.sha256(value["body"]).hexdigest()
                        for key, value in remote.objects.items()
                        if key.startswith(prefix)
                    ],
                }
            )
        return result

    app.router.routes.extend(frontend_routes)
    return app, engine


def main():
    from tests.database import require_test_database

    base = os.environ.get("DATABASE_URL", "")
    require_test_database(base)
    source = make_url(base)
    if source.host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("Browser acceptance requires a loopback PostgreSQL service")
    name = "r2_browser_" + uuid4().hex[:16] + "_test"
    owner = BACKEND.parent / ".runtime/r2-browser-acceptance" / name
    owner.mkdir(parents=True, mode=0o700)
    ownership = owner / "ownership.json"
    admin_url = source.set(
        database="postgres", drivername="postgresql"
    ).render_as_string(hide_password=False)
    with psycopg.connect(admin_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    ownership.write_text(
        json.dumps(
            {"database": name, "created_by_this_process": True, "pid": os.getpid()}
        )
    )
    os.environ.update(
        {
            "DATABASE_URL": source.set(database=name).render_as_string(
                hide_password=False
            ),
            "SECRET_KEY": secrets.token_urlsafe(48),
            "FIRST_SUPERUSER": "synthetic-r2-bootstrap",
            "FIRST_SUPERUSER_PASSWORD": secrets.token_urlsafe(32),
            "PROJECT_NAME": "R2 Browser Acceptance",
            "FRONTEND_HOST": APP_ORIGIN,
            "MATERIAL_INGEST_ENABLED": "true",
            "MATERIAL_CLEANUP_ENABLED": "false",
            "OBJECT_STORAGE_PROVIDER": "r2",
            "S3_ENDPOINT_URL": "https://browser-test.r2.cloudflarestorage.com",
            "S3_BUCKET": "synthetic-browser-bucket",
            "S3_ACCESS_KEY_ID": "synthetic-browser-key",
            "S3_SECRET_ACCESS_KEY": "synthetic-browser-signing-secret",
            "TIKTOK_APP_ID": "",
            "TIKTOK_APP_SECRET": "",
            "REDIS_URL": "redis://127.0.0.1:1/15",
            "AWS_EC2_METADATA_DISABLED": "true",
        }
    )
    engine = None
    previous_directory = Path.cwd()
    try:
        with TemporaryDirectory(prefix="r2-browser-tls-") as temporary:
            directory = Path(temporary)
            # Settings reads ../.env by default. A private empty parent prevents
            # this acceptance process from loading any deployment .env at all.
            isolated_cwd = directory / "work"
            isolated_cwd.mkdir()
            os.chdir(isolated_cwd)
            clip = directory / "tiny.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=160x240:d=0.2",
                    "-an",
                    "-c:v",
                    "mpeg4",
                    str(clip),
                ],
                check=True,
                capture_output=True,
                timeout=15,
            )
            remote = StorageWire()
            gateway = storage_server(remote, directory)
            try:
                app, engine = create_app(remote, clip.read_bytes())

                # Uvicorn re-raises SIGTERM after shutdown. Convert it into a
                # Python unwind so this process can release only its owned DB.
                def terminate(_signum, _frame):
                    raise KeyboardInterrupt

                signal.signal(signal.SIGTERM, terminate)
                uvicorn.run(
                    app,
                    host="127.0.0.1",
                    port=5293,
                    access_log=False,
                    log_level="warning",
                )
            finally:
                gateway.shutdown()
                gateway.server_close()
                remote.signer.close()
    finally:
        os.chdir(previous_directory)
        if engine is not None:
            engine.dispose()
        with psycopg.connect(admin_url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )
        ownership.write_text(
            json.dumps(
                {
                    "database": name,
                    "created_by_this_process": True,
                    "dropped": True,
                    "pid": os.getpid(),
                }
            )
        )


if __name__ == "__main__":
    main()
