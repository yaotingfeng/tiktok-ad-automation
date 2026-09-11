"""新任务的纯只读消费不访问凭据或写入数据库。"""

from sqlalchemy import event, text
from sqlmodel import Session

from app.modules.builds.scene import read_scene_context
from tests.modules.builds.scene.support import enqueue, ensure, run, scene_responses


def test_scene_reader_uses_only_select_in_postgres_readonly_transaction(
    database_engine, redis_client, scene_case, gateway_wire
):
    case = scene_case
    receipt = ensure(database_engine, case)
    for resource, data in scene_responses(case).items():
        enqueue(gateway_wire, resource, data)
        job = run(database_engine, redis_client, case, receipt.job_id)
    assert job.status == "COMPLETE", job.error_code
    statements = []

    def capture(_conn, _cursor, statement, _params, _ctx, _many):
        statements.append(statement.lstrip().split()[0])

    event.listen(database_engine, "before_cursor_execute", capture)
    try:
        with Session(database_engine) as db, db.begin():
            db.execute(text("SET TRANSACTION READ ONLY"))
            result = read_scene_context(
                db,
                context=case["context"],
                bc_id=case["route"].bc_id,
                advertiser_id=case["advertiser_id"],
                link_id=case["link_id"],
                route=case["route"],
            )
            assert result.supported
    finally:
        event.remove(database_engine, "before_cursor_execute", capture)
    assert set(statements) == {"SELECT", "SET"}
