"""素材回传的生产谱系：只接受已鉴权来源及本租户真实订单，不按剧名猜测。"""

from uuid import UUID

from sqlmodel import Session

from app.core.errors import DomainError
from app.modules.ai_workbench.order_models import MaterialOrder


def production_payload(order: MaterialOrder) -> dict:
    """统一使用素材制作 /api/schedules 已冻结合同中的 payload，不推测其他层级。"""
    value = order.request_body.get("payload", {})
    return value if isinstance(value, dict) else {}


def validate_production_origin(session: Session, *, tenant_id: UUID, origin) -> dict:
    order = session.get(MaterialOrder, origin.order_id)
    if (
        order is None
        or order.tenant_id != tenant_id
        or not order.job_id
        or order.job_id != origin.job_id
        or origin.strategy_id
        not in production_payload(order).get("selectedStrategyIds", [])
    ):
        raise DomainError(
            "material_production_origin_invalid", "素材生产来源与租户订单不匹配"
        )
    return {
        "order_id": str(order.id),
        "job_id": order.job_id,
        "strategy_id": origin.strategy_id,
        "source": "authenticated_producer",
    }
