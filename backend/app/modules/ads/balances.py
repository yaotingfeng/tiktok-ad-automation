"""余额独立观测写入；不混入周期指标，调用方持有事务和当前冻结路由。"""

from dataclasses import asdict

from sqlmodel import Session

from app.core.context import TenantContext
from app.integrations.tiktok.contracts.ads import AccountBalance
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.modules.accounts.routing import verify_route
from app.modules.reporting.models import AccountBalanceObservation


def persist_balance(
    session: Session,
    *,
    context: TenantContext,
    route: FrozenTikTokRoute,
    advertiser_id: str,
    balance: AccountBalance,
) -> AccountBalanceObservation:
    verify_route(
        session,
        context=context,
        route=route,
        advertiser_id=advertiser_id,
        capability="read",
    )
    if balance.balance_scope == "ADVERTISER" and balance.scope_id != advertiser_id:
        raise ValueError("balance scope does not match advertiser")
    observation = AccountBalanceObservation(
        tenant_id=route.tenant_id,
        advertiser_id=advertiser_id,
        amount=balance.amount,
        currency=balance.currency,
        availability=balance.availability,
        balance_scope=balance.balance_scope,
        scope_id=balance.scope_id,
        observed_at=balance.observed_at,
        evidence={
            "call": asdict(balance.evidence),
            "route": route.model_dump(mode="json"),
        },
        source_connection_id=route.connection_id,
        source_channel=route.channel,
    )
    session.add(observation)
    session.flush()
    return observation
