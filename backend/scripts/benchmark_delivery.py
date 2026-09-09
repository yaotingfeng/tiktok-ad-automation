"""Offline real outbox + bounded expansion deliveries, separate from DB-only capacity."""


# ruff: noqa: T201 -- the benchmark emits only bounded synthetic progress metrics.

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter, sleep
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

from sqlalchemy import event, func, update
from sqlalchemy.orm import Session as SASession
from sqlmodel import Session, col, select

from app.core.config import settings
from app.jobs import outbox, tasks
from app.jobs.celery_app import celery_app
from app.jobs.models import PendingDispatch
from app.modules.builds.execution_models import ExecutionStep, Submission
from app.modules.builds.submission_tasks import process_submission
from app.modules.builds.submissions import submit_preview
from scripts.benchmark_batches import Parameters, Recorder, owned_database, rss_bytes
from scripts.benchmark_support import (
    Transport,
    bootstrap,
    freeze,
    offline_runtime,
    prepare,
    seed_inventory,
)


def measure(
    *, accounts: int, dramas: int, cadence: float, publisher: str = "single-round"
) -> dict[str, Any]:
    parameters = Parameters(accounts=accounts, dramas=dramas, target_accounts=accounts)
    recorder = Recorder(parameters)
    transport = Transport()
    result: dict[str, Any] = {
        "schema": "p07-expansion-delivery-v2",
        "source_revision": recorder.report["source_revision"],
        "source_dirty": recorder.report["source_dirty"],
        "environment": recorder.report["environment"],
        "accounts": accounts,
        "dramas": dramas,
        "cadence_seconds": cadence,
        "publisher": publisher,
        "publisher_max_rounds": settings.DISPATCH_MAX_ROUNDS
        if publisher == "drain"
        else 1,
        "publisher_time_budget_seconds": settings.DISPATCH_TIME_BUDGET_SECONDS
        if publisher == "drain"
        else None,
        "complete": False,
        "boundary": "Real SDK GET transports, preparation, preview, outbox, current delivery ID and expansion DB pages. Broker captured locally. Ordinary execution messages accepted but not executed. No advertising API writes.",
    }
    with (
        owned_database(str(settings.DATABASE_URL)) as engine,
        offline_runtime(transport) as redis_client,
    ):
        scopes, previews = [], []
        for label, args in (
            ("delivery-t1", parameters),
            ("delivery-t2", Parameters(accounts=2, dramas=1, target_accounts=2)),
        ):
            scope = seed_inventory(engine, args, label=label)
            transport.scopes[scope.bc_id] = scope
            bootstrap(engine, scope, args.target_accounts, redis_client, recorder)
            draft = prepare(engine, scope, args, transport, recorder)
            previews.append(freeze(engine, scope, draft, recorder))
            scopes.append(scope)
        # All earlier preparation bodies were consumed synchronously above.
        with Session(engine) as session, session.begin():
            SASession.execute(
                session, update(PendingDispatch).values(published_at=datetime.now(UTC))
            )
            large = submit_preview(
                session,
                context=scopes[0].context,
                preview_id=previews[0],
                request_id=uuid4(),
            ).submission_id
        small: UUID | None = None
        t2_start: float | None = None
        query_profiles: dict[str, dict[str, Any]] = {}

        def query_start(
            connection: Any,
            _cursor: Any,
            _statement: Any,
            _parameters: Any,
            _context: Any,
            _many: Any,
        ) -> None:
            connection.info["capacity_query_started"] = perf_counter()

        def query_end(
            connection: Any,
            _cursor: Any,
            statement: str,
            _parameters: Any,
            _context: Any,
            _many: Any,
        ) -> None:
            elapsed = perf_counter() - connection.info.pop("capacity_query_started")
            # Bind values are never stored. The bounded prefix identifies shape.
            shape = " ".join(statement.split())[:1200]
            profile = query_profiles.setdefault(
                shape, {"calls": 0, "seconds": 0.0, "max_seconds": 0.0}
            )
            profile["calls"] += 1
            profile["seconds"] += elapsed
            profile["max_seconds"] = max(profile["max_seconds"], elapsed)

        event.listen(engine, "before_cursor_execute", query_start)
        event.listen(engine, "after_cursor_execute", query_end)
        start = previous_round = perf_counter()
        round_number, deliveries = 0, []
        t2_seconds = None
        messages: list[dict[str, Any]] = []
        publications: list[dict[str, Any]] = []

        def broker(name: str, **kwargs: Any) -> None:
            messages.append({"name": name, **kwargs})

        def publish(*, tail: bool = False) -> int:
            messages.clear()
            started = perf_counter()
            with (
                patch.object(outbox, "engine", engine),
                patch.object(celery_app, "send_task", broker),
            ):
                count = (
                    tasks.drain_dispatch(limit=100)
                    if publisher == "drain"
                    else outbox.flush_dispatch(limit=100)
                )
            elapsed = perf_counter() - started
            assert count == len(messages)
            assert len({message["task_id"] for message in messages}) == count
            publications.append(
                {
                    "tail_only": tail,
                    "seconds": elapsed,
                    "messages": count,
                    "ordinary_messages": sum(
                        message["name"] != "builds.expand_submission"
                        for message in messages
                    ),
                    "tenants": {
                        f"T{index + 1}": sum(
                            message["kwargs"]["tenant_id"]
                            == str(scope.context.tenant_id)
                            for message in messages
                        )
                        for index, scope in enumerate(scopes)
                    },
                }
            )
            return count

        while True:
            if round_number:
                sleep(max(0.0, cadence - (perf_counter() - previous_round)))
            previous_round = perf_counter()
            round_number += 1
            published = publish()
            for message in messages:
                if message["name"] != "builds.expand_submission":
                    continue
                payload = message["kwargs"]["payload"]
                identity = UUID(payload["submission_id"])
                started = perf_counter()
                process_submission(
                    database_engine=engine,
                    tenant_id=UUID(message["kwargs"]["tenant_id"]),
                    actor_id=UUID(message["kwargs"]["actor_id"]),
                    payload=payload,
                    dispatch_id=UUID(message["task_id"]),
                )
                duration = perf_counter() - started
                with Session(engine) as session:
                    row = session.get(Submission, identity)
                    assert row
                    steps = session.exec(
                        select(func.count())
                        .select_from(ExecutionStep)
                        .where(ExecutionStep.submission_id == identity)
                    ).one()
                    done = row.expanded
                delivery = {
                    "round": round_number,
                    "tenant": "T1" if identity == large else "T2",
                    "revision": payload["revision"],
                    "seconds": duration,
                    "cumulative_steps": steps,
                    "expanded": done,
                }
                deliveries.append(delivery)
                print(json.dumps({"phase": "delivery", **delivery}), flush=True)
                if identity == small and done and t2_seconds is None:
                    assert t2_start is not None
                    t2_seconds = perf_counter() - t2_start
            if small is None:
                # Inject a genuinely smaller expansion after T1 has generated its
                # first real unit backlog; both tenants then use the same outbox.
                with Session(engine) as session, session.begin():
                    backlog = session.exec(
                        select(func.count())
                        .select_from(PendingDispatch)
                        .where(
                            PendingDispatch.tenant_id == scopes[0].context.tenant_id,
                            col(PendingDispatch.published_at).is_(None),
                        )
                    ).one()
                    small = submit_preview(
                        session,
                        context=scopes[1].context,
                        preview_id=previews[1],
                        request_id=uuid4(),
                    ).submission_id
                result["t1_backlog_at_t2_submission"] = backlog
                t2_start = perf_counter()
            with Session(engine) as session:
                row = session.get(Submission, large)
                assert row
                if row.expanded and t2_seconds is not None:
                    break
            if not published or round_number > 200:
                raise AssertionError("Expansion deliveries stopped advancing")
        result.update(
            seconds=perf_counter() - start,
            rounds=round_number,
            deliveries=deliveries,
            t2_complete_seconds=t2_seconds,
            peak_rss_bytes=rss_bytes(),
            query_profiles=[
                {"statement_shape": shape, **values}
                for shape, values in sorted(
                    query_profiles.items(),
                    key=lambda item: item[1]["seconds"],
                    reverse=True,
                )[:10]
            ],
        )
        with Session(engine) as session:
            result["steps_by_kind"] = dict(
                session.exec(
                    select(ExecutionStep.kind, func.count())
                    .where(ExecutionStep.submission_id == large)
                    .group_by(ExecutionStep.kind)
                ).all()
            )
        assert sum(result["steps_by_kind"].values()) == accounts * dramas * 51
        # Drain only genuinely generated ordinary unit dispatches after expansion.
        # No worker executes those messages and no remote request is made.
        tail_start = perf_counter()
        if publisher == "drain":
            for _ in range(2000):
                with Session(engine) as session:
                    pending = session.exec(
                        select(PendingDispatch.id)
                        .where(col(PendingDispatch.published_at).is_(None))
                        .limit(1)
                    ).first()
                if pending is None:
                    break
                sleep(max(0.0, cadence - (perf_counter() - previous_round)))
                previous_round = perf_counter()
                if not publish(tail=True):
                    raise AssertionError("Remaining ordinary publication stopped")
            else:
                raise AssertionError("Tail publication exceeded benchmark bound")
        result["tail_publication_seconds"] = perf_counter() - tail_start
        result["publications"] = publications
        result["peak_rss_bytes"] = rss_bytes()
    result["complete"] = True
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--accounts", type=int, default=100)
    parser.add_argument("--dramas", type=int, default=10)
    parser.add_argument("--cadence", type=float)
    parser.add_argument(
        "--publisher", choices=("single-round", "drain"), default="single-round"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cadence = (
        args.cadence
        if args.cadence is not None
        else (1.0 if args.publisher == "drain" else 5.0)
    )
    if args.accounts < 1 or args.dramas < 1 or not 0 < cadence <= 60:
        parser.error("invalid bounded benchmark parameters")
    result = measure(
        accounts=args.accounts,
        dramas=args.dramas,
        cadence=cadence,
        publisher=args.publisher,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                key: value
                for key, value in result.items()
                if key not in {"deliveries", "query_profiles", "publications"}
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
