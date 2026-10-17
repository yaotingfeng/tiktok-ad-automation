"""规则取数问题的统一说明；逐次及汇总通知均不把部分素材缺数说成全账户过期。"""

MATERIAL_REASONS = {
    "MATERIAL_LINK_MISSING": "素材与报表尚未关联",
    "MATERIAL_LINK_UNRESOLVED": "历史帖子尚未准确关联",
    "MATERIAL_REPORT_UNLINKED": "报表有尚未定位的帖子，单广告表现完整性无法确认",
    "MATERIAL_SHARED_REPORT": "多条广告共用报表无法拆分",
    "MATERIAL_IDENTITY_AMBIGUOUS": "当前素材身份缺失或不唯一",
    "MATERIAL_METRIC_MISSING": "判断指标不完整",
}


def input_issue_lines(items: list[dict[str, str]]) -> list[str]:
    stale = {
        item["advertiser_id"] for item in items if item.get("reason") == "STALE_REPORT"
    }
    lines = [f"{len(stale)} 个账户数据未更新，已跳过"] if stale else []
    for item in items:
        reason = MATERIAL_REASONS.get(item.get("reason", ""))
        if reason:
            lines.append(
                f"账户 {item['advertiser_id']}：{item.get('count', '部分')} 个广告内素材无法判断，{reason}"
            )
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
