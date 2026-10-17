"""规则取数问题的统一说明；逐次及汇总通知均不把部分素材缺数说成全账户过期。"""

MATERIAL_REASONS = {
    "MATERIAL_LINK_MISSING": "素材与报表尚未关联（可能未投放或尚未同步）",
    "MATERIAL_LINK_UNRESOLVED": "历史帖子尚未准确关联",
    "MATERIAL_REPORT_UNLINKED": "报表有尚未定位的帖子，单广告表现完整性无法确认",
    "MATERIAL_SHARED_REPORT": "多条广告共用报表无法拆分",
    "MATERIAL_IDENTITY_AMBIGUOUS": "当前素材身份缺失或不唯一",
    "MATERIAL_METRIC_MISSING": "判断指标不完整",
    "MATERIAL_PARENT_UNKNOWN": "上级广告目录或状态不完整",
}


def input_issue_lines(items: list[dict[str, str]]) -> list[str]:
    stale = {
        item["advertiser_id"] for item in items if item.get("reason") == "STALE_REPORT"
    }
    lines = [f"{len(stale)} 个账户数据未更新，已跳过"] if stale else []
    # 同类问题按账户去重聚合，避免几十条账户 ID 淹没止损结果。
    grouped: dict[str, list[dict[str, str]]] = {}
    for item in merge_input_issues(items):
        if item.get("reason") in MATERIAL_REASONS:
            grouped.setdefault(item["reason"], []).append(item)
    ordered = sorted(grouped, key=lambda reason: (-len(grouped[reason]), reason))
    for reason in ordered[:2]:
        group = grouped[reason]
        count = (
            str(sum(int(item["count"]) for item in group))
            if all(item.get("count", "").isdigit() for item in group)
            else "部分"
        )
        lines.append(
            f"{count} 个广告内素材未参与判断（{len(group)} 个账户）：{MATERIAL_REASONS[reason]}"
        )
    if len(ordered) > 2:
        lines.append(f"另有 {len(ordered) - 2} 类缺数问题，详见运行记录")
    return lines


def merge_input_issues(items: list[dict[str, str]]) -> list[dict[str, str]]:
    """跨运行以同账户同原因的最大数量呈现，不把反复跳过相加成素材数量。"""
    merged: dict[tuple[str, str], dict[str, str]] = {}
    for item in items:
        key = (item["advertiser_id"], item.get("reason", ""))
        previous = merged.get(key)
        if previous is None or int(item.get("count", "0")) > int(
            previous.get("count", "0")
        ):
            merged[key] = item
    return [merged[key] for key in sorted(merged)]
