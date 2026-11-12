"""真实 PostgreSQL：重叠的控制任务只能领取同一剧单一次。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import uuid4
from zoneinfo import ZoneInfo

import psycopg
import pytest
from psycopg import sql
from sqlalchemy.engine import make_url
from sqlmodel import Session, create_engine, select

from app.core.config import settings
from app.jobs.models import PendingDispatch
from app.modules.ai_workbench import plans_service, service
from app.modules.ai_workbench.build_handoff import TASK_NAME, BuildHandoff
from app.modules.ai_workbench.build_tasks import dispatch_due_rows
from app.modules.ai_workbench.models import BuildOccurrence
from app.modules.ai_workbench.plan_schemas import DailyPlanRequest
from app.modules.ai_workbench.schemas import OptimizerCreate
from tests.database import require_test_database
from tests.database_template import create_head_database
from tests.modules.conftest import create_context

from .test_daily_plans import _plan_body, _provider_application
from .test_optimizer import create_body, seed_optimizer_dependencies


@pytest.fixture
def dispatch_database():
    source = make_url(str(settings.DATABASE_URL))
    require_test_database(str(settings.DATABASE_URL))
    name = f"ai_dispatch_{uuid4().hex}_test"
    create_head_database(str(settings.DATABASE_URL), name)
    database = create_engine(source.set(database=name))
    try:
        with Session(database) as session:
            context = create_context(session)
            strategy = seed_optimizer_dependencies(session, context)
            optimizer = service.create_optimizer(
                session,
                context=context,
                body=OptimizerCreate.model_validate(create_body(strategy.id)),
            )
            connection, _ = _provider_application(session, context)
            local = datetime.now(ZoneInfo("Asia/Shanghai")).replace(
                second=0, microsecond=0
            )
            body = _plan_body(
                optimizer.id,
                connection.id,
                business_date=str(local.date()),
                scheduled_time=local.strftime("%H:%M"),
            )
            body["business_date"] = str(local.date())
            plan = plans_service.create_daily_plan(
                session,
                context=context,
                optimizer_id=optimizer.id,
                body=DailyPlanRequest.model_validate(body),
            )
            session.commit()
            row_id = plan.rows[0].id
        yield database, row_id, local.astimezone(UTC) + timedelta(seconds=1)
    finally:
        database.dispose()
        admin = source.set(drivername="postgresql", database="postgres")
        with psycopg.connect(
            admin.render_as_string(hide_password=False), autocommit=True
        ) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


def test_overlapping_dispatchers_skip_locked_and_enqueue_once(
    dispatch_database, monkeypatch
):
    database, row_id, dispatch_time = dispatch_database
    locked, release = Event(), Event()
    original = BuildHandoff.create_occurrence

    def held(handoff, *args, **kwargs):
        locked.set()
        assert release.wait(timeout=10)
        return original(handoff, *args, **kwargs)

    monkeypatch.setattr(BuildHandoff, "create_occurrence", held)

    def work():
        with Session(database) as session, session.begin():
            return dispatch_due_rows(session, now=dispatch_time)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(work)
        assert locked.wait(timeout=10)
        second = pool.submit(work)
        try:
            assert second.result(timeout=10) == 0
        finally:
            release.set()
        assert first.result(timeout=10) == 1
    with Session(database) as session:
        assert (
            len(
                session.exec(
                    select(BuildOccurrence).where(BuildOccurrence.plan_row_id == row_id)
                ).all()
            )
            == 1
        )
        assert (
            len(
                session.exec(
                    select(PendingDispatch).where(
                        PendingDispatch.task_name == TASK_NAME
                    )
                ).all()
            )
            == 1
        )
