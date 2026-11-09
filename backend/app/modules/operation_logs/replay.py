"""基于可验证的前后值链还原日最低ROAS；不以当前值猜历史。"""

from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, TypedDict

NEGATIVE = {
    "CAMPAIGN_PAUSED",
    "CAMPAIGN_DELETED",
    "BUDGET_DECREASED",
    "ADGROUP_MIN_BUDGET_CHANGED",
    "MANUAL_BID_LIMIT",
}
BID_FIELDS = {"minimum roas", "target roas", "roas bid", "roas_bid"}


class BidChange(TypedDict):
    at: datetime
    before: Decimal
    after: Decimal


def number(value: object) -> Decimal | None:
    try:
        # 仅明确ROAS数值，不把百分数或任意币种字段当倍数。
        result = Decimal(str(value).strip())
        return result if result.is_finite() and result > 0 else None
    except InvalidOperation, TypeError, ValueError:
        return None


def ordered_chain(
    changes: list[BidChange], value: Decimal, *, reverse: bool
) -> list[BidChange] | None:
    """分钟内按前后值连链；出现多条合法顺序即拒绝猜测。"""
    if len(changes) > 8:
        return None
    solutions: list[list[BidChange]] = []

    def walk(left: list[int], current: Decimal, path: list[BidChange]) -> None:
        if len(solutions) > 1:
            return
        if not left:
            solutions.append(path)
            return
        for i in left:
            change = changes[i]
            expected = change["after" if reverse else "before"]
            if expected == current:
                walk(
                    [j for j in left if j != i],
                    change["before" if reverse else "after"],
                    path + [change],
                )

    walk(list(range(len(changes))), value, [])
    return solutions[0] if len(solutions) == 1 else None


def restore_bid(
    anchor: Decimal | None,
    anchor_at: datetime,
    start: datetime,
    end: datetime,
    changes: list[BidChange],
) -> Decimal | None:
    if anchor is None:
        return None
    # 官方日志精确到分钟，观察落在同一分钟不能证明该变化已生效。
    if any(
        c["at"].replace(second=0, microsecond=0)
        == anchor_at.replace(second=0, microsecond=0)
        for c in changes
    ):
        return None
    if start < anchor_at < end:
        before = restore_bid(anchor, anchor_at, start, anchor_at, changes)
        after = restore_bid(anchor, anchor_at, anchor_at, end, changes)
        return min(before, after) if before is not None and after is not None else None
    reverse = anchor_at >= end
    lower, upper = (start, anchor_at) if reverse else (anchor_at, end)
    grouped: dict[datetime, list[BidChange]] = defaultdict(list)
    for c in changes:
        if lower <= c["at"] < upper:
            grouped[c["at"]].append(c)
    value, candidates = anchor, []
    start_value = anchor
    for stamp in sorted(grouped, reverse=reverse):
        ordered = ordered_chain(grouped[stamp], value, reverse=reverse)
        if ordered is None:
            return None
        for change in ordered:
            if start <= stamp < end:
                candidates.extend([change["before"], change["after"]])
            value = change["before" if reverse else "after"]
            if not reverse and stamp < start:
                start_value = value
    candidates.append(value if reverse else start_value)
    return min(candidates) if all(v is not None and v > 0 for v in candidates) else None


def evaluate_day(
    *,
    spend: Decimal | None,
    events: Decimal | None,
    revenue: Decimal | None,
    bid: Decimal | None,
    reasons: list[str],
) -> dict[str, Any]:
    reason_set = set(reasons)
    if "STRUCTURE_UNSUPPORTED" in reason_set and spend != 0:
        return {"amount": None, "status": "UNSUPPORTED", "reasons": sorted(reason_set)}
    if spend == 0 or events is not None and events < 300 or reason_set & NEGATIVE:
        return {"amount": "0", "status": "ZERO", "reasons": sorted(reason_set)}
    if any(v is None for v in (spend, events, revenue, bid)) or reason_set:
        return {
            "amount": None,
            "status": "UNKNOWN",
            "reasons": sorted(reason_set or {"NUMERIC_INPUT_MISSING"}),
        }
    assert bid is not None and spend is not None and revenue is not None
    if bid * spend <= Decimal("1.2") * revenue:
        return {"amount": "0", "status": "ZERO", "reasons": ["THRESHOLD_NOT_MET"]}
    return {
        "amount": str(max(Decimal(0), spend - revenue / bid)),
        "status": "ESTIMATED_PENDING",
        "reasons": ["PLATFORM_QUALIFICATION_PENDING"],
    }
