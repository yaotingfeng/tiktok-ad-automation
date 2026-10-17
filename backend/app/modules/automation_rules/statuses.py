"""状态条件与素材恢复保护共用同一条租户内、账户内的上级链。"""

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlmodel import Session

PARENT_KIND = {"ad": "adgroup", "adgroup": "campaign"}


def known_status(value: str | None) -> str | None:
    return value if value in ("ENABLE", "DISABLE") else None


def hierarchy_statuses(
    row: Any, objects: dict[tuple[str, str, str], Any]
) -> dict[str, str | None]:
    result: dict[str, str | None] = dict.fromkeys(
        ("campaign_status", "adgroup_status", "ad_status")
    )
    while row is not None:
        result[f"{row.kind}_status"] = known_status(row.operation_status)
        parent_kind = PARENT_KIND.get(row.kind)
        row = (
            objects.get((row.advertiser_id, parent_kind, row.parent_remote_id))
            if parent_kind
            else None
        )
    return result


def blocked_material_recoveries(
    session: Session, tenant_id: UUID, targets: dict[str, dict[str, Any]]
) -> set[str]:
    """执行前重新读取目录，不能沿用命中快照里已经失效的上级状态。

    批量加载所涉账户的目录，避免逐素材查询；不请求平台或写入广告。
    """
    materials = {
        key: target for key, target in targets.items() if target["kind"] == "material"
    }
    if not materials:
        return set()
    rows = (
        session.connection()
        .execute(
            text(
                "SELECT advertiser_id,kind,remote_id,parent_remote_id,operation_status "
                "FROM ad_object WHERE tenant_id=:t AND advertiser_id=ANY(:advs) "
                "AND kind IN ('campaign','adgroup','ad')"
            ),
            {
                "t": tenant_id,
                "advs": sorted({t["advertiser_id"] for t in materials.values()}),
            },
        )
        .all()
    )
    objects = {(r.advertiser_id, r.kind, r.remote_id): r for r in rows}
    blocked = set()
    for key, target in materials.items():
        uses = target.get("material_uses", [])
        if not uses:
            blocked.add(key)
        for use in uses:
            row = objects.get((target["advertiser_id"], "ad", use["ad_remote_id"]))
            if (
                use["advertiser_id"] != target["advertiser_id"]
                or row is None
                or any(
                    status != "ENABLE"
                    for status in hierarchy_statuses(row, objects).values()
                )
            ):
                blocked.add(key)
                break
    return blocked
