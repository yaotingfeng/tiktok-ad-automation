"""共享契约：源和每个目标必须在同一发送授权下可操作。"""

from collections.abc import Callable
from typing import Any

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.materials import AssetShare


def distribution_transport(*, source_bc_id: str, target_bc_id: str) -> str:
    if not source_bc_id or not target_bc_id:
        raise DomainError("material_source_scope_missing", "素材来源 BC 不明确")
    return "native_share" if source_bc_id == target_bc_id else "url_relay"


def share_arguments(
    request: AssetShare, *, authorize: Callable[[str], None] | None
) -> dict[str, Any]:
    if (
        authorize is None
        or type(request.material_ids) is not tuple
        or type(request.shared_advertiser_ids) is not tuple
        or request.asset_type not in {"VIDEO", "IMAGE", "MUSIC"}
        or not 1 <= len(request.material_ids) <= 20
        or not 1 <= len(request.shared_advertiser_ids) <= 10
        or len(set(request.material_ids)) != len(request.material_ids)
        or len(set(request.shared_advertiser_ids)) != len(request.shared_advertiser_ids)
        or request.advertiser_id in request.shared_advertiser_ids
        or any(
            not isinstance(value, str) or not value.strip()
            for value in (
                request.advertiser_id,
                *request.material_ids,
                *request.shared_advertiser_ids,
            )
        )
    ):
        raise DomainError("material_share_invalid", "素材共享参数或账户授权边界无效")
    for account in (request.advertiser_id, *request.shared_advertiser_ids):
        authorize(account)
    return {
        "advertiser_id": request.advertiser_id,
        "asset_type": request.asset_type,
        "material_ids": list(request.material_ids),
        "shared_advertiser_ids": list(request.shared_advertiser_ids),
    }
