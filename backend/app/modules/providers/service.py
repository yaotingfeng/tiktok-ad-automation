"""Public provider preparation entrypoints; caller owns the transaction."""

from typing import Any
from uuid import UUID

from sqlmodel import Session

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.core.pagination import Page
from app.modules.providers.schemas import LinkResultStatus, ResolvedLink


def clean_lines(lines: list[str]) -> list[tuple[int, str]]:
    if (
        not isinstance(lines, list)
        or len(lines) > 1000
        or any(not isinstance(raw, str) or len(raw) > 1000 for raw in lines)
    ):
        raise DomainError(
            "provider_request_invalid", "请输入最多 1000 行剧名，每行最多 1000 字符"
        )
    result, seen = [], set()
    for number, raw in enumerate(lines, start=1):
        value = raw.strip()
        if value and value not in seen:
            result.append((number, value))
            seen.add(value)
    return result


def prepare_links(
    session: Session,
    *,
    context: TenantContext,
    connection_id: UUID,
    application_id: str,
    lines: list[str],
    config: dict[str, Any],
    request_id: UUID,
) -> UUID:
    from app.modules.providers.repository import create_preparation_request

    return create_preparation_request(
        session,
        context=context,
        connection_id=connection_id,
        application_id=application_id,
        lines=lines,
        config=config,
        request_id=request_id,
    )


def get_link_results(
    session: Session,
    *,
    context: TenantContext,
    task_id: UUID,
    cursor: str | None = None,
    page_size: int = 100,
    status: LinkResultStatus | None = None,
    exceptions_only: bool = False,
) -> Page[ResolvedLink]:
    from app.modules.providers.repository import read_preparation_results

    return read_preparation_results(
        session,
        context=context,
        task_id=task_id,
        cursor=cursor,
        page_size=page_size,
        status=status,
        exceptions_only=exceptions_only,
    )


def choose_drama_candidate(
    session: Session,
    *,
    context: TenantContext,
    input_id: UUID,
    external_drama_id: str,
) -> UUID:
    from app.modules.providers.repository import select_preparation_candidate

    return select_preparation_candidate(
        session,
        context=context,
        input_id=input_id,
        external_drama_id=external_drama_id,
    )
