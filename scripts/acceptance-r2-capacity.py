"""Repeatable synthetic FastAPI/JWT/PostgreSQL metadata capacity acceptance.

Requires a dedicated *_test database. Never sends bytes or operations to R2 or
TikTok. Own tenant fixtures are removed in finally, including their exact budget
delta. This is not a live throughput or complete mixed-failure worker benchmark.
"""

import argparse

# Command-line stdout intentionally emits only sanitized JSON.
# ruff: noqa: T201
import json
import math
import sys
import time
from collections import defaultdict
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


class MetadataStorage:
    """Explicit transport seam: only multipart initialization has a remote twin."""

    def __init__(self):
        self.created: dict[str, str] = {}

    def create_multipart_upload(self, **values):
        identity = uuid4().hex
        self.created[identity] = values["Key"]
        return {"UploadId": identity}

    def close(self):
        pass


def metrics(samples: list[tuple[float, int]]) -> dict[str, Any]:
    durations = sorted(milliseconds for milliseconds, _ in samples)
    queries = [count for _, count in samples]
    return {
        "requests": len(samples),
        "p50_ms": round(durations[math.ceil(len(durations) * 0.5) - 1], 3),
        "p95_ms": round(durations[math.ceil(len(durations) * 0.95) - 1], 3),
        "sql_min": min(queries),
        "sql_max": max(queries),
        "sql_total": sum(queries),
    }


def run_capacity(*, count: int, database_url: str | None = None) -> dict[str, Any]:
    from tests.database import require_test_database

    # Reject an explicitly supplied production URL before importing settings,
    # app/engine, running migrations, creating a client or touching a fixture.
    if database_url is not None:
        require_test_database(database_url)
    from app.core.config import settings

    require_test_database(str(settings.DATABASE_URL))
    if database_url is not None and database_url != str(settings.DATABASE_URL):
        raise ValueError(
            "Capacity runtime must use its configured dedicated test database"
        )
    if type(count) is not int or not 1 <= count <= 20_000:
        raise ValueError("Capacity count must be between 1 and 20000")

    from app.core.db import engine
    from app.core.security import create_access_token
    from app.main import app
    from app.models import User
    from app.modules.accounts.models import TenantBC
    from app.modules.materials import storage
    from app.modules.materials.ingest_models import (
        ObjectBudget,
        ObjectCleanup,
        TemporaryMaterialObject,
    )
    from app.modules.materials.object_budget import release_object_reservation
    from app.modules.tenants.models import Tenant, TenantMembership
    from fastapi.testclient import TestClient
    from sqlalchemy import delete, event
    from sqlmodel import Session, SQLModel, col, select

    run_id, tenant_id, actor_id = uuid4().hex, uuid4(), uuid4()
    bc_id = "capacity-" + run_id
    base = f"/api/tenants/{tenant_id}/materials/ingest-sessions"
    wire = MetadataStorage()
    sql_calls: list[None] = []
    samples: dict[str, list[tuple[float, int]]] = defaultdict(list)
    started = time.perf_counter()
    report: dict[str, Any] = {
        "mode": "synthetic_fastapi_jwt_postgresql",
        "run_id": run_id,
        "files": count,
        "external_business_calls": 0,
        "mixed_faults_1000": {
            "status": "pending",
            "reason": "Requires integrated source/target/cleanup worker fault harness",
        },
        "source_account_fairness": {"status": "pending"},
        "worker_memory": {
            "status": "not_measured",
            "reason": "No worker or file-byte workload in this metadata harness",
        },
        "live_daily_throughput": {"status": "pending"},
    }

    def observe(*_args):
        sql_calls.append(None)

    def request(client, category, method, url, **values):
        before, clock = len(sql_calls), time.perf_counter()
        response = client.request(method, url, **values)
        samples[category].append(
            ((time.perf_counter() - clock) * 1000, len(sql_calls) - before)
        )
        if response.status_code not in {200, 201}:
            raise RuntimeError(
                f"Capacity {category} returned HTTP {response.status_code}"
            )
        return response.json()

    def parent(client, file_count, byte_size):
        return request(
            client,
            "create",
            "POST",
            base,
            json={
                "bc_id": bc_id,
                "request_id": str(uuid4()),
                "file_count": file_count,
                "total_bytes": file_count * byte_size,
            },
        )

    try:
        with Session(engine) as db, db.begin():
            db.add_all(
                [
                    User(
                        id=actor_id,
                        username="capacity-" + run_id,
                        hashed_password="unused-synthetic",
                    ),
                    Tenant(id=tenant_id, name="capacity-" + run_id),
                ]
            )
            db.flush()
            db.add_all(
                [
                    TenantMembership(
                        tenant_id=tenant_id, user_id=actor_id, role="operator"
                    ),
                    TenantBC(tenant_id=tenant_id, bc_id=bc_id),
                ]
            )
        event.listen(engine, "before_cursor_execute", observe)
        with ExitStack() as overrides:
            # Deterministic local-only namespace; never reuse operational values.
            for name, value in {
                "MATERIAL_INGEST_ENABLED": True,
                "MATERIAL_CLEANUP_ENABLED": False,
                "OBJECT_STORAGE_PROVIDER": "r2",
                "S3_ENDPOINT_URL": "https://capacity.r2.cloudflarestorage.com",
                "S3_BUCKET": "capacity-synthetic",
                "S3_ACCESS_KEY_ID": "synthetic-capacity-access",
                "S3_SECRET_ACCESS_KEY": "synthetic-capacity-secret",
            }.items():
                overrides.enter_context(patch.object(settings, name, value))
            overrides.enter_context(
                patch.object(storage, "make_object_s3", lambda _obj: wire)
            )
            overrides.enter_context(
                patch.object(
                    storage,
                    "make_s3",
                    side_effect=AssertionError(
                        "External storage is disabled in capacity acceptance"
                    ),
                )
            )
            with TestClient(app) as client:
                client.headers["Authorization"] = "Bearer " + create_access_token(
                    actor_id, timedelta(hours=1)
                )
                created = parent(client, count, 100)
                session_url = base + "/" + created["session_id"]
                receipts = []
                expected: dict[int, str] = {}
                for start in range(0, count, 200):
                    body = {
                        "request_id": str(uuid4()),
                        "files": [
                            {
                                "client_index": index,
                                "file_name": f"{run_id}-{index}.mp4",
                                "size": 100,
                                "mime_type": "video/mp4",
                                "last_modified_ms": index + 1,
                            }
                            for index in range(start, min(start + 200, count))
                        ],
                    }
                    result = request(
                        client, "chunk", "POST", session_url + "/chunks", json=body
                    )
                    receipts.append((body, result))
                    expected.update(
                        {
                            row["client_index"]: row["material_id"]
                            for row in result["items"]
                        }
                    )
                # Recreate the client as a browser/API connection restart. JWT,
                # receipt and manifest identities are recovered from PostgreSQL.
            with TestClient(app) as client:
                client.headers["Authorization"] = "Bearer " + create_access_token(
                    actor_id, timedelta(hours=1)
                )
                replay = request(
                    client,
                    "receipt_replay",
                    "POST",
                    session_url + "/chunks",
                    json=receipts[0][0],
                )
                if replay["items"] != receipts[0][1]["items"]:
                    raise AssertionError("Chunk identities changed after reconnect")
                seal = request(client, "seal", "POST", session_url + "/seal")
                if not seal["sealed"]:
                    raise AssertionError("Complete manifest did not seal")
                seen: dict[int, str] = {}
                cursor = None
                pages = 0
                while True:
                    page = request(
                        client,
                        "page",
                        "GET",
                        session_url + "/files",
                        params={"limit": 100, **({"cursor": cursor} if cursor else {})},
                    )
                    pages += 1
                    if len(page["items"]) > 100:
                        raise AssertionError("Page exceeded server bound")
                    for row in page["items"]:
                        if row["client_index"] in seen:
                            raise AssertionError("Duplicate client index across pages")
                        seen[row["client_index"]] = row["material_id"]
                    if not page["next_cursor"]:
                        break
                    if page["next_cursor"] == cursor or not page["items"]:
                        raise AssertionError("Pagination did not advance")
                    cursor = page["next_cursor"]
                if seen != expected or len(set(seen.values())) != count:
                    raise AssertionError("Pagination lost or changed file identities")
                for _ in range(5):
                    summary = request(client, "summary", "GET", session_url)
                    if (
                        summary["accepted_count"] != count
                        or "files" in summary
                        or summary["reserved_bytes"] != 0
                    ):
                        raise AssertionError(
                            "Summary changed facts or allocated metadata storage"
                        )
                report.update(chunks=len(receipts), pages=pages, unique_files=len(seen))
                report.update(
                    {name: metrics(values) for name, values in samples.items()}
                )
                report["synthetic_acceptance_files_per_second"] = round(
                    count / (sum(value for value, _ in samples["chunk"]) / 1000), 3
                )

                # Separate synthetic accounting case: the storage double emits
                # only Create receipts; no bytes/validator/provider are involved.
                with Session(engine) as db:
                    budget = db.get(ObjectBudget, "global")
                    global_before = budget.reserved_bytes if budget else 0
                window = 256
                overrides.enter_context(
                    patch.object(
                        settings,
                        "MATERIAL_STORAGE_GLOBAL_BYTES",
                        global_before + window,
                    )
                )
                overrides.enter_context(
                    patch.object(settings, "MATERIAL_STORAGE_TENANT_BYTES", window)
                )
                created = parent(client, 3, 128)
                budget_url = base + "/" + created["session_id"]
                rows = request(
                    client,
                    "budget",
                    "POST",
                    budget_url + "/chunks",
                    json={
                        "request_id": str(uuid4()),
                        "files": [
                            {
                                "client_index": index,
                                "file_name": f"{run_id}-budget-{index}.mp4",
                                "size": 128,
                                "mime_type": "video/mp4",
                            }
                            for index in range(3)
                        ],
                    },
                )["items"]
                resumed = []
                for row in rows:
                    resumed.append(
                        request(
                            client,
                            "budget",
                            "POST",
                            budget_url + "/files/" + row["material_id"] + "/resume",
                            json={
                                name: row[name]
                                for name in (
                                    "generation",
                                    "upload_id",
                                    "operation_revision",
                                )
                            },
                        )
                    )
                blocked = (
                    resumed[-1]["temporary_storage_status"] == "waiting_capacity"
                    and resumed[-1]["upload_id"] is None
                )
                if not blocked or len(wire.created) != 2:
                    raise AssertionError("Admission did not enforce the bounded window")
                peak = request(client, "budget", "GET", budget_url)["reserved_bytes"]
                # This fixture represents a independently verified deletion.
                # It does not exercise, or claim success for, the cleanup worker.
                with Session(engine) as db, db.begin():
                    obj = db.exec(
                        select(TemporaryMaterialObject).where(
                            col(TemporaryMaterialObject.tenant_id) == tenant_id,
                            col(TemporaryMaterialObject.material_id)
                            == UUID(rows[0]["material_id"]),
                        )
                    ).one()
                    assert obj.s3_upload_id is not None
                    wire.created.pop(obj.s3_upload_id)
                    obj.status = "deleted"
                    obj.deleted_at = datetime.now(UTC)
                    proof = ObjectCleanup(
                        tenant_id=tenant_id,
                        bc_id=bc_id,
                        material_id=obj.material_id,
                        generation=obj.generation,
                        reason="synthetic_capacity_evidence",
                        status="deleted",
                        abort_confirmed_at=obj.deleted_at,
                        head_confirmed_at=obj.deleted_at,
                        eligibility_evidence={
                            "mode": "synthetic_fixture_not_live_cleanup"
                        },
                    )
                    db.add(proof)
                    db.flush()
                    release_object_reservation(
                        db, object_id=obj.id, deletion_evidence_id=proof.id
                    )
                resumed_after = request(
                    client,
                    "budget",
                    "POST",
                    budget_url + "/files/" + rows[-1]["material_id"] + "/resume",
                    json={
                        name: resumed[-1][name]
                        for name in ("generation", "upload_id", "operation_revision")
                    },
                )
                restored = resumed_after[
                    "temporary_storage_status"
                ] == "receiving" and bool(resumed_after["upload_id"])
                if not restored:
                    raise AssertionError(
                        "Admission did not resume after proven release"
                    )
                report["backpressure"] = {
                    "mode": "synthetic_transport_and_deletion_evidence",
                    "tenant_limit_bytes": window,
                    "reserved_peak_bytes": peak,
                    "stored_peak_bytes": 0,
                    "waiting_observed": blocked,
                    "resumed_after_evidence": restored,
                    "cleanup_worker_chain": "pending",
                }
    finally:
        if event.contains(engine, "before_cursor_execute", observe):
            event.remove(engine, "before_cursor_execute", observe)
        # Scope cleanup to this randomly created tenant and its exact global
        # occupancy contribution; never truncate/reset budgets of other tests.
        with Session(engine) as db, db.begin():
            global_budget = db.exec(
                select(ObjectBudget)
                .where(col(ObjectBudget.scope_key) == "global")
                .with_for_update()
            ).one_or_none()
            tenant_budget = db.get(ObjectBudget, f"tenant:{tenant_id}")
            if tenant_budget and global_budget:
                global_budget.reserved_bytes -= tenant_budget.reserved_bytes
                global_budget.stored_bytes -= tenant_budget.stored_bytes
                db.flush()
            for table in reversed(SQLModel.metadata.sorted_tables):
                if "tenant_id" in table.c:
                    db.exec(delete(table).where(table.c.tenant_id == tenant_id))
            db.exec(delete(Tenant).where(col(Tenant.id) == tenant_id))
            db.exec(delete(User).where(col(User.id) == actor_id))
        wire.created.clear()
        report["cleanup"] = {"fixture_removed": True}
    report["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, choices=(10_000, 20_000), required=True)
    parser.add_argument(
        "--output", type=Path, help="Write synthetic metrics JSON to this new file"
    )
    parser.add_argument(
        "--scenario", choices=("metadata", "mixed-faults-1000"), default="metadata"
    )
    args = parser.parse_args(argv)
    if args.scenario != "metadata":
        print(
            json.dumps(
                {
                    "scenario": args.scenario,
                    "status": "pending",
                    "reason": "Integrated worker fault harness is required",
                }
            )
        )
        return 2
    try:
        report = run_capacity(count=args.count)
        rendered = json.dumps(report, indent=2, sort_keys=True)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x") as target:
                target.write(rendered + "\n")
        print(rendered)
        return 0
    except Exception as error:
        # ORM/HTTP exception text can include connection/authorization data.
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(error).__name__,
                    "details": "Inspect dedicated test environment and rerun focused tests",
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
