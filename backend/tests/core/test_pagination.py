import pytest
from pydantic import ValidationError
from sqlalchemy import literal, select
from sqlmodel import Session

from app.core.pagination import Page, count_rows


def test_page_serializes_exact_total() -> None:
    page = Page[int](items=[1, 2], next_cursor="next", total=3)

    assert page.model_dump() == {
        "items": [1, 2],
        "next_cursor": "next",
        "total": 3,
    }


@pytest.mark.parametrize("payload", [{"items": []}, {"items": [], "total": -1}])
def test_page_requires_non_negative_total(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Page[int].model_validate(payload)


def test_count_rows_counts_the_unpaginated_statement(session: Session) -> None:
    statement = select(literal(1)).union_all(select(literal(2)))

    assert count_rows(session, statement) == 2
