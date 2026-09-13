"""推广链接到 TikTok Mini 的确认记录；不以版权方应用作为投放目标。"""

from datetime import UTC, datetime
from hashlib import sha256
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from sqlalchemy import CheckConstraint, Column, DateTime
from sqlmodel import Field, Session, SQLModel

from app.core.context import TenantContext
from app.modules.providers.models import PromotionLink


class MiniTarget(SQLModel, table=True):
    __tablename__ = "build_mini_target"
    __table_args__ = (
        CheckConstraint(
            "source IN ('USER','LINK','LEGACY')", name="ck_build_mini_target_source"
        ),
    )
    tenant_id: UUID = Field(foreign_key="tenant.id", primary_key=True)
    url_digest: str = Field(primary_key=True, max_length=64)
    minis_id: str = Field(max_length=128)
    source: str = Field(max_length=16)
    actor_id: UUID | None = Field(default=None, foreign_key="user.id")
    confirmed_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


def url_key(url: str) -> str:
    return sha256(url.encode()).hexdigest()


def explicit_mini_id(url: str) -> str | None:
    try:
        parts = urlsplit(url)
        values = parse_qs(parts.query, keep_blank_values=True).get("minis_id", [])
        if (
            parts.scheme != "https"
            or parts.hostname not in {"www.tiktok.com", "tiktok.com"}
            or parts.username
            or parts.password
        ):
            return None
        if (
            len(values) == 1
            and 1 <= len(values[0]) <= 128
            and all(c.isascii() and (c.isalnum() or c in "_-.") for c in values[0])
        ):
            return values[0]
    except ValueError:
        pass
    return None


def resolved_target(
    session: Session, *, context: TenantContext, url: str
) -> str | None:
    direct = explicit_mini_id(url)
    if direct:
        return direct
    saved = session.get(MiniTarget, (context.tenant_id, url_key(url)))
    return saved.minis_id if saved else None


def matching_link_id(url: str, available_ids: set[str]) -> str | None:
    direct = explicit_mini_id(url)
    if direct:
        return direct if direct in available_ids else None
    try:
        parts = urlsplit(url)
        if (
            parts.scheme == "https"
            and parts.hostname in {"www.tiktok.com", "tiktok.com"}
            and not parts.username
            and not parts.password
        ):
            path = parts.path.rstrip("/").split("/")
            if len(path) == 3 and path[1] == "minis" and path[2] in available_ids:
                return path[2]
    except ValueError:
        pass
    return None


def remember_target(
    session: Session, *, context: TenantContext, url: str, minis_id: str, source: str
) -> None:
    from sqlalchemy.dialects.postgresql import insert

    statement = insert(MiniTarget).values(
        tenant_id=context.tenant_id,
        url_digest=url_key(url),
        minis_id=minis_id,
        source=source,
        actor_id=context.actor_id,
        confirmed_at=datetime.now(UTC),
    )
    session.exec(
        statement.on_conflict_do_update(
            index_elements=["tenant_id", "url_digest"],
            set_={
                key: getattr(statement.excluded, key)
                for key in ("minis_id", "source", "actor_id", "confirmed_at")
            },
        )
    )


def link_url(link: PromotionLink) -> str:
    from app.core.errors import DomainError

    if (
        not isinstance(link.url, str)
        or not link.url
        or link.status != "ready"
        or not link.verified_at
    ):
        raise DomainError("scene_link_unavailable", "推广链接需要重新准备")
    return link.url
