"""余额最新观测按账户短探测；成本与账户数有关，不随历史记录数增长。"""

from collections.abc import Collection
from uuid import UUID

from sqlalchemy import select, text
from sqlmodel import Session

from app.modules.reporting.models import AccountBalanceObservation


def latest_balance_observations(
    session: Session, *, tenant_id: UUID, advertisers: Collection[str]
) -> tuple[AccountBalanceObservation, ...]:
    accounts = sorted(set(advertisers))
    if not accounts:
        return ()
    # DISTINCT ON 会先扫描所选账户的全部历史；此处沿既有历史索引每户取最新一条。
    # 并列时间仍按ID决胜；先取最新记录，再解释availability，不能复活旧可用余额。
    statement = select(AccountBalanceObservation).from_statement(
        text("""
        SELECT latest.*
        FROM unnest(CAST(:advertisers AS text[])) k(advertiser_id)
        CROSS JOIN LATERAL (
            SELECT o.* FROM account_balance_observation o
            WHERE o.tenant_id=:tenant AND o.advertiser_id=k.advertiser_id
            ORDER BY o.observed_at DESC,o.id DESC LIMIT 1
        ) latest
        ORDER BY k.advertiser_id
        """)
    )
    return tuple(
        session.scalars(statement, {"tenant": tenant_id, "advertisers": accounts}).all()
    )
