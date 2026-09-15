from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlmodel import Session, select


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None = None
    total: int = Field(ge=0)


def count_rows(session: Session, statement: Any) -> int:
    """Count a fully scoped query before callers add cursor and limit clauses."""
    count = select(func.count()).select_from(statement.order_by(None).subquery())
    return int(session.exec(count).one())
