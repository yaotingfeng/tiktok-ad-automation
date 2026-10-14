"""规则操作到广告管理变更的映射，以及频控、相反动作的判断。"""

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from app.modules.ad_management.schemas import MutationField, MutationSpec, StatusValue
from app.modules.automation_rules.schemas import Action, Frequency

_FIELD: dict[str, MutationField] = {"BUDGET": "budget", "ROAS": "roas"}
_SUFFIX = {"INCREASE": "UP", "DECREASE": "DOWN", "SET": "SET"}
_WINDOWS = {
    "EVERY_24H": timedelta(hours=24),
    "EVERY_48H": timedelta(hours=48),
    "EVERY_WEEK": timedelta(days=7),
}
_UNITS = {"MINUTE": "minutes", "HOUR": "hours", "DAY": "days"}


def action_key(action: Action) -> str:
    if action.type in ("PAUSE", "ENABLE"):
        return action.type
    assert action.adjustment is not None
    return f"{action.type}_{_SUFFIX[action.adjustment.mode]}"


def opposite(a: str, b: str) -> bool:
    """暂停与启用相反；同一字段的提高与降低相反；「调整至」与同字段任何调整都视为相反。"""
    if {a, b} == {"PAUSE", "ENABLE"}:
        return True
    field_a, _, kind_a = a.partition("_")
    field_b, _, kind_b = b.partition("_")
    if not kind_a or not kind_b or field_a != field_b:
        return False
    return "SET" in (kind_a, kind_b) or {kind_a, kind_b} == {"UP", "DOWN"}


def mutation_for(action: Action) -> MutationSpec:
    """不含目标引用的变更；目标由冻结选择提供。素材启停用 status + 素材使用表示。"""
    if action.type in ("PAUSE", "ENABLE"):
        value: StatusValue = "DISABLE" if action.type == "PAUSE" else "ENABLE"
        return MutationSpec(field="status", mode="set", value=value)
    adjustment = action.adjustment
    assert adjustment is not None
    field = _FIELD[action.type]
    if adjustment.mode == "SET":
        return MutationSpec(field=field, mode="set", value=adjustment.value)
    percent = adjustment.value_type == "PERCENT"
    if adjustment.mode == "INCREASE":
        return MutationSpec(
            field=field,
            mode="increase_percent" if percent else "add",
            value=adjustment.value,
            max_value=adjustment.limit,
        )
    return MutationSpec(
        field=field,
        mode="decrease_percent" if percent else "subtract",
        value=adjustment.value,
        min_value=adjustment.limit,
    )


def frequency_allows(
    frequency: Frequency | None, history: list[datetime], now: datetime
) -> bool:
    """同一规则对同一对象的执行次数上限；history 为该规则对该对象此前的执行时间。"""
    if frequency is None:
        return True
    if frequency.type == "ONCE":
        return not history
    if frequency.type in _WINDOWS:
        return all(moment <= now - _WINDOWS[frequency.type] for moment in history)
    assert frequency.unit and frequency.window and frequency.count
    window = timedelta(**{_UNITS[frequency.unit]: frequency.window})
    return sum(1 for moment in history if moment > now - window) < frequency.count


def group_by_bc(targets: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """执行按 BC 分别走各自的冻结路由；BC 顺序稳定，便于审计与重放。"""
    groups: dict[str, list[dict[str, Any]]] = {}
    for target in sorted(targets, key=lambda t: (t["bc_id"], t["key"])):
        groups.setdefault(target["bc_id"], []).append(target)
    return groups


def _ref(
    tenant_id: UUID, advertiser_id: str, kind: str, remote_id: str
) -> dict[str, str]:
    return {
        "tenant_id": str(tenant_id),
        "advertiser_id": advertiser_id,
        "kind": kind,
        "remote_id": remote_id,
    }


def selection_payload(
    targets: list[dict[str, Any]], tenant_id: UUID
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """对象目标写入 refs；素材目标展开为它在各广告中的全部使用。"""
    refs: list[dict[str, str]] = []
    uses: list[dict[str, Any]] = []
    for target in targets:
        if target["kind"] != "material":
            refs.append(
                _ref(
                    tenant_id,
                    target["advertiser_id"],
                    target["kind"],
                    target["remote_id"],
                )
            )
            continue
        for use in target.get("material_uses", ()):
            uses.append(
                {
                    "ad_ref": _ref(
                        tenant_id, use["advertiser_id"], "ad", use["ad_remote_id"]
                    ),
                    "platform_material_id": use["platform_material_id"],
                    "ad_material_id": use["ad_material_id"],
                    "material_type": use["material_type"],
                }
            )
    return refs, uses
