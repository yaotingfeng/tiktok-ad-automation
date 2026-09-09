"""Loopback-only real FastAPI acceptance server with external-wire fixtures.

Run from backend with a disposable *_test database and TEST_REDIS_URL. Production
API handlers are unchanged. Test controls seed inventory and deliver actual
registered tasks; the optional lost-response fault runs AFTER a real commit.
"""

import json
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock
from typing import Any, Literal
from uuid import UUID, uuid4

import uvicorn
from alembic import command
from alembic.config import Config
from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlmodel import Session, select

from app.api.deps import get_db
from app.core.config import settings
from app.core.db import engine
from app.main import FRONTEND_DIR, app
from app.modules.accounts.models import AdvertiserAccount
from app.modules.builds.execution_models import Submission
from tests.acceptance.scenario import PASSWORD, Scope, Wire, offline_runtime, seed_scope
from tests.database import require_test_database

require_test_database(str(settings.DATABASE_URL))
wire = Wire()
lock = Lock()
scopes: dict[str, Scope] = {}
runtime: Any = None
requests: list[dict[str, Any]] = []
lose_submission = False


@asynccontextmanager
async def lifespan(_app):
    global runtime
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[2] / "app/alembic")
    )
    command.upgrade(config, "head")
    with offline_runtime(wire, engine) as active:
        runtime = active
        yield


app.router.lifespan_context = lifespan


def database():
    with Session(engine) as session:
        yield session


app.dependency_overrides[get_db] = database
# Keep the existing production SPA fallback after test control routes.
frontend_routes = [
    route
    for route in app.router.routes
    if getattr(route, "path", None) in {"", "/{path:path}"}
]
for route in frontend_routes:
    app.router.routes.remove(route)


class DeliveryFault:
    def __init__(self, application):
        self.application = application

    async def __call__(self, scope, receive, send):
        global lose_submission
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            return await self.application(scope, receive, send)
        submission = scope["method"] == "POST" and scope["path"].endswith("/submit")
        request_body = bytearray()
        response_messages = []
        entry = {
            "method": scope["method"],
            "path": scope["path"],
            "query": scope.get("query_string", b"").decode(),
        }
        requests.append(entry)

        async def read():
            message = await receive()
            if submission:
                request_body.extend(message.get("body", b""))
            return message

        async def capture(message):
            if message["type"] == "http.response.start":
                entry["status"] = message["status"]
            if submission:
                response_messages.append(message)
            else:
                await send(message)

        await self.application(scope, read, capture)
        if not submission:
            return
        entry["request_id"] = json.loads(request_body or b"{}").get("request_id")
        if lose_submission and entry.get("status") == 202:
            lose_submission = False
            entry["delivery_lost"] = True
            body = b'{"detail":"Synthetic response delivery interruption"}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 503,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
        else:
            for message in response_messages:
                await send(message)


app.add_middleware(DeliveryFault)


class ScenarioRequest(BaseModel):
    partial_currency: bool = False
    provider_kind: Literal["jiashu", "wangyan"] = "jiashu"


@app.get("/__acceptance__/health")
def health():
    return {"ready": runtime is not None}


@app.post("/__acceptance__/scenario")
def scenario(options: ScenarioRequest):
    with lock:
        label = "browser-" + uuid4().hex[:10]
        scope = seed_scope(engine, wire, label=label, provider_kind=options.provider_kind)
        other = seed_scope(engine, wire, label=label + "-other", material_count=1)
        scopes[str(scope.context.tenant_id)] = scope
        scopes[str(other.context.tenant_id)] = other
        if options.partial_currency:
            with Session(engine) as session, session.begin():
                account = session.get(
                    AdvertiserAccount, (scope.context.tenant_id, scope.accounts[-1])
                )
                assert account
                account.currency = "EUR"
        return {
            "tenant_id": str(scope.context.tenant_id),
            "bc_id": scope.bc_id,
            "email": scope.email,
            "password": PASSWORD,
            "accounts": scope.accounts,
            "other_tenant_id": str(other.context.tenant_id),
            "other_bc_id": other.bc_id,
        }


@app.post("/__acceptance__/pump")
def pump():
    with lock:
        return {"delivered": runtime.pump_jobs(), "diagnostics": runtime.diagnostics()}


@app.post("/__acceptance__/lose-submission-response")
def lose_response():
    global lose_submission
    lose_submission = True
    return {"armed": True}


@app.get("/__acceptance__/evidence/{tenant_id}")
def evidence(tenant_id: UUID):
    if str(tenant_id) not in scopes:
        raise HTTPException(404)
    with Session(engine) as session:
        count = session.exec(
            select(func.count())
            .select_from(Submission)
            .where(Submission.tenant_id == tenant_id)
        ).one()
    return {
        "requests": [r for r in requests if f"/tenants/{tenant_id}/" in r["path"]],
        "submission_count": count,
        "sdk_calls": dict(wire.calls),
        "smart_posts": len([c for c in wire.smart.calls if c["method"] == "POST"]),
    }


if not (FRONTEND_DIR / "index.html").is_file():
    raise RuntimeError("Build the frontend before starting acceptance server")
app.router.routes.extend(frontend_routes)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=5191, access_log=False, log_level="warning")
