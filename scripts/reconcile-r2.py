"""Inspect one explicit tenant/BC maintenance page; --enqueue only writes intents.

Default mode reads PostgreSQL. --mode objects or multipart performs one
read-only R2 prefix list. No mode performs object deletion, abort or budget release. Persist the
returned cursor and pass it to the next invocation; reset after an empty page.
"""

# CLI stdout intentionally contains sanitized diagnostic JSON.
# ruff: noqa: T201
import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def execute(args: argparse.Namespace) -> dict[str, Any]:
    from app.core.config import settings
    from app.core.context import TenantContext
    from app.core.db import engine
    from app.core.errors import DomainError
    from app.modules.materials.cleanup_reconcile import (
        scan_abandoned_objects,
        scan_r2_orphans,
    )
    from app.modules.materials.storage import make_s3
    from app.modules.materials.uploads import require_bc
    from app.modules.tenants.permissions import require_tenant
    from sqlmodel import Session

    context = TenantContext(
        tenant_id=args.tenant_id, actor_id=args.actor_id, role="operator"
    )
    if args.mode == "database":
        with Session(engine) as db, db.begin():
            return scan_abandoned_objects(
                db,
                context=context,
                bc_id=args.bc_id,
                cursor=args.cursor,
                limit=args.limit,
                enqueue=args.enqueue,
            )
    # Validate current authority before even constructing a credentialed client.
    with Session(engine) as db:
        action = "upload" if args.enqueue else "read"
        require_tenant(
            db, actor_id=context.actor_id, tenant_id=context.tenant_id, action=action
        )
        require_bc(db, context=context, bc_id=args.bc_id, action=action)
    snapshot = settings.model_copy()
    if snapshot.OBJECT_STORAGE_PROVIDER != "r2":
        raise DomainError("object_storage_unconfigured", "核查需要明确R2配置")
    snapshot.require_object_storage()
    s3 = make_s3(snapshot)
    try:
        return scan_r2_orphans(
            database_engine=engine,
            context=context,
            bc_id=args.bc_id,
            s3=s3,
            storage_provider=snapshot.OBJECT_STORAGE_PROVIDER,
            storage_endpoint=snapshot.S3_ENDPOINT_URL,
            storage_bucket=snapshot.S3_BUCKET,
            cursor=args.cursor,
            kind="multipart" if args.mode == "multipart" else "objects",
            limit=args.limit,
            enqueue=args.enqueue,
        )
    finally:
        s3.close()


def main(
    argv: list[str] | None = None, *, executor: Callable[..., dict[str, Any]] = execute
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True, type=UUID)
    parser.add_argument("--actor-id", required=True, type=UUID)
    parser.add_argument("--bc-id", required=True)
    parser.add_argument(
        "--mode", choices=("database", "objects", "multipart"), default="database"
    )
    parser.add_argument("--cursor")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument(
        "--enqueue",
        action="store_true",
        help="Explicitly queue eligible cleanup intents; requires cleanup switch and upload authority",
    )
    args = parser.parse_args(argv)
    try:
        if not 1 <= len(args.bc_id) <= 128 or not 1 <= args.limit <= 100:
            raise ValueError("invalid scoped window")
        result = executor(args)
        print(json.dumps(result, sort_keys=True))
        return 2 if result.get("disabled") else 0
    except Exception as error:
        print(json.dumps({"status": "failed", "error_type": type(error).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
