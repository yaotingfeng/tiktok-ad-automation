"""Transactional outbox with at-least-once delivery and bounded tenant rounds.

Celery task_id is an identity, not automatic deduplication. A consumer must use
its own database business key to make repeated execution safe. Large workflows
must enqueue a bounded executable window instead of their entire future graph.
"""

import json
import math
import re
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn, cast
from uuid import UUID, uuid4

from sqlalchemy import literal
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.db import engine
from app.core.errors import DomainError
from app.jobs.celery_app import celery_app
from app.jobs.models import DispatchTenantCursor, PendingDispatch
from app.jobs.tasks import dispatch_queue

# One expansion slot keeps a generated successor ahead of its own unit fanout.
# Other expansion messages never consume ordinary FIFO slots in this round.
_EXPANSION_TASK = "builds.expand_submission"

_SECRET_NAMES = {
    "token",
    "cookie",
    "password",
    "secret",
    "authorization",
    "credential",
    "filebody",
    "filecontent",
    "filedata",
    "filebytes",
    "filebase64",
    "videobody",
    "videocontent",
    "videodata",
    "videobytes",
    "body",
    "content",
}


def validate_dispatch_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Accept small JSON references/scalars, never credentials or file bodies."""

    def invalid() -> NoReturn:
        raise DomainError(
            "dispatch_payload_invalid", "任务参数只能包含内部标识和必要标量"
        )

    def check(value: object, depth: int = 0) -> None:
        if depth > 8:
            invalid()
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str) or len(key) > 128:
                    invalid()
                normalized = re.sub(r"[^a-z0-9]", "", key.lower())
                if any(word in normalized for word in _SECRET_NAMES):
                    invalid()
                check(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                check(child, depth + 1)
        elif isinstance(value, str):
            if len(value) > 1024:
                invalid()
        elif value is None or isinstance(value, (bool, int)):
            pass
        elif isinstance(value, float) and math.isfinite(value):
            pass
        else:
            invalid()

    if not isinstance(payload, dict):
        invalid()
    check(payload)
    try:
        encoded = json.dumps(payload, allow_nan=False)
    except TypeError, ValueError, OverflowError:
        invalid()
    if len(encoded.encode()) > 16384:
        invalid()
    # Snapshot caller-owned objects: later mutation cannot change the dispatch.
    return cast(dict[str, Any], json.loads(encoded))


def enqueue_after_commit(
    session: Session,
    *,
    context: TenantContext,
    task_name: str,
    task_key: str,
    payload: dict[str, Any],
) -> UUID:
    """Write in the caller's transaction. Never commit or publish here."""
    dispatch_queue(task_name)
    payload = validate_dispatch_payload(payload)
    if not isinstance(task_key, str) or not task_key.strip() or len(task_key) > 255:
        raise DomainError("dispatch_payload_invalid", "任务标识无效")
    record_id = uuid4()
    session.exec(
        insert(DispatchTenantCursor)
        .values(tenant_id=context.tenant_id)
        .on_conflict_do_nothing(index_elements=["tenant_id"])
    )
    result = session.exec(
        insert(PendingDispatch)
        .values(
            id=record_id,
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            task_name=task_name,
            task_key=task_key,
            payload=payload,
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "task_key"])
        .returning(col(PendingDispatch.id))
    ).first()
    if result is not None:
        return cast(UUID, result[0])
    existing = session.exec(
        select(PendingDispatch).where(
            PendingDispatch.tenant_id == context.tenant_id,
            PendingDispatch.task_key == task_key,
        )
    ).one()
    if (existing.task_name, existing.actor_id) != (
        task_name,
        context.actor_id,
    ) or json.dumps(existing.payload, sort_keys=True) != json.dumps(
        payload, sort_keys=True
    ):
        raise DomainError("dispatch_key_conflict", "同一任务标识的配置不一致")
    return existing.id


def flush_dispatch(limit: int = 100) -> int:
    """One tenant round: at most one expansion, then ordinary FIFO; <=5/tenant.

    With only expansions due we deliberately leave the other slots unused.
    A second expansion cannot starve ordinary work as new requests arrive.
    Broker failures retain their original delivery IDs and normal backoff.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("Dispatch limit must be a positive integer")
    limit = min(limit, 100)
    now = datetime.now(UTC)
    published = 0
    attempted = 0
    with Session(engine) as session, session.begin():
        due = (
            select(col(PendingDispatch.id))
            .where(
                PendingDispatch.tenant_id == col(DispatchTenantCursor.tenant_id),
                col(PendingDispatch.published_at).is_(None),
                col(PendingDispatch.available_at) <= now,
            )
            .exists()
        )
        cursors = session.exec(
            select(DispatchTenantCursor)
            .where(due)
            .order_by(
                col(DispatchTenantCursor.last_published_at).asc().nulls_first(),
                col(DispatchTenantCursor.tenant_id),
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for cursor in cursors:
            if attempted >= limit:
                break
            slots = min(5, limit - attempted)
            pending = (
                select(PendingDispatch)
                .where(
                    PendingDispatch.tenant_id == cursor.tenant_id,
                    col(PendingDispatch.published_at).is_(None),
                    col(PendingDispatch.available_at) <= now,
                )
                .order_by(col(PendingDispatch.available_at), col(PendingDispatch.id))
                .with_for_update(skip_locked=True)
            )
            records = list(
                session.exec(
                    # A fixed literal preserves the partial-index proof when
                    # PostgreSQL switches a prepared query to a generic plan.
                    pending.where(
                        PendingDispatch.task_name
                        == literal(_EXPANSION_TASK, literal_execute=True)
                    ).limit(1)
                ).all()
            )
            ordinary_slots = slots - len(records)
            if ordinary_slots:
                records.extend(
                    session.exec(
                        pending.where(
                            PendingDispatch.task_name != _EXPANSION_TASK
                        ).limit(ordinary_slots)
                    ).all()
                )
            for record in records:
                attempted += 1
                record.attempts += 1
                try:
                    queue = dispatch_queue(record.task_name)
                    payload = validate_dispatch_payload(record.payload)
                    celery_app.send_task(
                        record.task_name,
                        kwargs={
                            "tenant_id": str(record.tenant_id),
                            "actor_id": str(record.actor_id),
                            "payload": payload,
                        },
                        task_id=str(record.id),
                        queue=queue,
                    )
                except Exception:
                    # Do not persist broker exceptions: they can contain secrets.
                    record.available_at = datetime.now(UTC) + timedelta(
                        seconds=min(300, 2 ** min(record.attempts, 8))
                    )
                else:
                    record.published_at = datetime.now(UTC)
                    published += 1
            if records:
                # Serviced attempts advance the round even during broker outages.
                cursor.last_published_at = datetime.now(UTC)
    return published
