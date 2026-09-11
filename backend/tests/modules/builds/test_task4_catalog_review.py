"""Independent read-only preparation catalog verification with real PostgreSQL."""

from uuid import uuid4

from sqlalchemy import event, text
from sqlmodel import Session, select

from app.modules.builds import catalog
from app.modules.builds.drafts import create_draft, prepare_draft
from app.modules.builds.models import DraftDrama
from tests.modules.builds.test_drafts import account, create_intent, finish, ready_links
from tests.modules.materials.test_tenant_materials import material


def test_preparation_catalog_pages_are_complete_and_sql_read_only(
    isolated_strategy_database, monkeypatch
):
    engine, context, _ = isolated_strategy_database
    with Session(engine) as session, session.begin():
        intent = create_intent(session, context)
        # 当前准备入口要求明确的 BC 绑定与默认连接，沿共享合成授权事实播种。
        account(session, context)
        expected = [
            material(session, context, f"Moon {i:03}.mp4", bc="bc-draft").id
            for i in range(205)
        ]
        shared = material(session, context, "Moon Short Drama.mp4", bc="bc-draft").id
        draft = create_draft(session, context=context, **intent)
        task = prepare_draft(
            session, context=context, draft_id=draft, request_id=uuid4()
        )
        ready_links(session, context, task, intent)
        finish(session, context, task)
        drama = (
            session.exec(
                select(DraftDrama).where(
                    DraftDrama.draft_id == draft, DraftDrama.title == "Moon"
                )
            )
            .one()
            .drama_id
        )
    statements = []

    def observe(_conn, _cursor, statement, _params, _ctx, _many):
        statements.append(statement)

    def forbidden(*_a, **_kw):
        raise AssertionError("read catalog crossed a preparation/remote boundary")

    with monkeypatch.context() as m:
        m.setattr("app.modules.builds.drafts.prepare_links", forbidden)
        m.setattr("app.jobs.outbox.enqueue_after_commit", forbidden)
        m.setattr("redis.Redis.execute_command", forbidden)
        m.setattr("boto3.client", forbidden)
        event.listen(engine, "before_cursor_execute", observe)
        try:
            with Session(engine) as session, session.begin():
                session.execute(text("SET TRANSACTION READ ONLY"))
                summary = catalog.draft_summary(
                    session, context=context, draft_id=draft
                )
                assert summary.status == "READY" and summary.drama_count == 2
                inputs = catalog.inputs_page(
                    session, context=context, draft_id=draft, kind="drama", limit=2
                )
                assert len(inputs.items) == 2 and inputs.next_cursor
                assert catalog.dramas_page(
                    session, context=context, draft_id=draft, limit=1
                ).next_cursor
                found, cursor, sizes = [], None, []
                while True:
                    page = catalog.materials_page(
                        session,
                        context=context,
                        draft_id=draft,
                        drama_id=drama,
                        cursor=cursor,
                        limit=100,
                    )
                    sizes.append(len(page.items))
                    found.extend(page.items)
                    cursor = page.next_cursor
                    if not cursor:
                        break
                assert sizes == [100, 100, 6]
                assert len({row.material_id for row in found}) == 206
                assert {row.material_id for row in found} == {*expected, shared}
                assert [
                    row.material_id for row in found if row.shared_with_other_drama
                ] == [shared]
                assert not session.new and not session.dirty and not session.deleted
        finally:
            event.remove(engine, "before_cursor_execute", observe)
    assert {query.split()[0].upper() for query in statements} <= {"SELECT", "SET"}
    material_queries = [q for q in statements if "JOIN material_file" in q]
    assert len(material_queries) == 3 and all("LIMIT" in q for q in material_queries)
