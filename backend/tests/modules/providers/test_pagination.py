import pytest
from sqlmodel import Session

from app.core.db import engine
from app.core.errors import DomainError
from app.modules.providers.service import get_link_results
from tests.modules.providers.test_preparation_api import (
    prepare,
)
from tests.modules.providers.test_preparation_api import (
    request_fixture as request_fixture,
)
from tests.modules.providers.test_preparation_api import (
    workflow as workflow,
)


@pytest.mark.parametrize(
    "page_size,sizes", [(100, [100, 100, 5]), (50, [50, 50, 50, 50, 5])]
)
def test_all_205_results_are_retrievable(request_fixture, page_size, sizes):
    task_id = prepare(request_fixture, lines=[f"Drama {n}" for n in range(205)])
    cursor, batches, numbers = None, [], []
    with Session(engine) as session:
        while True:
            page = get_link_results(
                session,
                context=request_fixture[0],
                task_id=task_id,
                cursor=cursor,
                page_size=page_size,
            )
            batches.append(len(page.items))
            numbers.extend(item.line_no for item in page.items)
            assert all(item.status == "pending" for item in page.items)
            assert "_work" not in repr(page.model_dump())
            cursor = page.next_cursor
            if cursor is None:
                break
    assert batches == sizes and numbers == list(range(1, 206))


def test_result_cursor_cannot_be_used_for_another_task(request_fixture):
    first = prepare(request_fixture, lines=[f"Drama {n}" for n in range(101)])
    second = prepare(request_fixture)
    with Session(engine) as session:
        cursor = get_link_results(
            session, context=request_fixture[0], task_id=first
        ).next_cursor
        with pytest.raises(DomainError) as error:
            get_link_results(
                session, context=request_fixture[0], task_id=second, cursor=cursor
            )
        assert error.value.code == "invalid_cursor"


@pytest.mark.parametrize("line_no", [True, 0, -1, "1"])
def test_malformed_result_cursor_is_rejected(request_fixture, line_no):
    import base64
    import json
    from uuid import uuid4

    task_id = prepare(request_fixture)
    cursor = base64.urlsafe_b64encode(
        json.dumps(
            {
                "scope": {
                    "tenant_id": str(request_fixture[0].tenant_id),
                    "task_id": str(task_id),
                    "kind": "results",
                },
                "line_no": line_no,
                "id": str(uuid4()),
            }
        ).encode()
    ).decode()
    with Session(engine) as session:
        with pytest.raises(DomainError) as error:
            get_link_results(
                session, context=request_fixture[0], task_id=task_id, cursor=cursor
            )
        assert error.value.code == "invalid_cursor"
