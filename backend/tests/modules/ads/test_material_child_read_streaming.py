"""读取冻结桥时每批回读，慢CPU不能让领取事务空闲十秒后被PG终止。"""

from copy import deepcopy
from time import sleep
from uuid import uuid4

import pytest
from psycopg import ServerCursor
from sqlalchemy import text

from app.core.errors import DomainError
from app.modules.ads import material_directory_children as children
from app.modules.ads.sync_models import AdDirectoryPage
from tests.modules.ads.test_material_child_context import child_case as child_case


def _bridges(session, child, *, corrupt_last=False):
    context = session.get(AdDirectoryPage, (child.id, children.MATERIAL_CONTEXT_PAGE))
    state = deepcopy(context.evidence[children.MATERIAL_CHILD_KEY])
    template = state["materials"][0]
    state["materials"] = [deepcopy(template) for _ in range(1001)]
    for index, material in enumerate(state["materials"]):
        material["use_ref"]["ad_material_id"] = f"material-{index}"
    if corrupt_last:
        state["materials"][-1]["use_ref"]["ad_ref"]["tenant_id"] = str(uuid4())
    context.evidence = {children.MATERIAL_CHILD_KEY: state}
    session.flush()
    session.expunge(context)


def test_material_read_batches_survive_real_idle_timeout(
    session, child_case, monkeypatch
):
    _, child = child_case
    _bridges(session, child)
    session.execute(text("SET LOCAL idle_in_transaction_session_timeout='10s'"))
    session.execute(text("SET LOCAL transaction_timeout='45s'"))
    original = children._usage
    original_fetchmany = ServerCursor.fetchmany
    count = 0
    fetched = []

    def bounded_fetchmany(cursor, size=0):
        rows = original_fetchmany(cursor, size)
        fetched.append((size, len(rows)))
        return rows

    def slow_usage(value):
        nonlocal count
        count += 1
        # 模拟每500条值对象构造的CPU耗时。整页急切读取会连续空闲12秒；
        # server cursor按批FETCH后每批均低于原10秒界限，不放宽任何超时。
        if count in {1, 501}:
            sleep(6)
        return original(value)

    monkeypatch.setattr(children, "_usage", slow_usage)
    monkeypatch.setattr(ServerCursor, "fetchmany", bounded_fetchmany)
    scope = children.material_child_scope(session, child)
    assert scope is not None and len(scope[1]) == count == 1001
    session.commit()
    assert len(fetched) >= 3
    assert max(size for size, _ in fetched) <= 500
    assert max(count for _, count in fetched) <= 500


@pytest.mark.parametrize("include_materials", [True, False])
def test_material_read_rejects_foreign_owner_after_batch_boundary(
    session, child_case, include_materials
):
    _, child = child_case
    _bridges(session, child, corrupt_last=True)
    with pytest.raises(DomainError, match="冻结范围"):
        children.material_child_scope(
            session, child, include_materials=include_materials
        )
