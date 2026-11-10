"""区分正常待数据与需要提醒的数据异常，原始问题仍保留在运行记录。"""

# 创意身份/上级目录待发布属于正常待同步；原帖子原因仅解释已保存的历史运行。
# 未关联创意金额、财务差额和来源不完整仍按数据异常提醒。无命中时逐轮推飞书
# 只会刷屏；对应素材仍不参与判断（不会误停），明细继续保留在运行记录与页面。
# 报表过期（STALE_REPORT）等真正的同步故障仍需提醒。
SYNC_PENDING_REASONS = frozenset(
    {
        "MATERIAL_CREATIVE_MISSING",
        "MATERIAL_LINK_MISSING",
        "MATERIAL_PARENT_UNKNOWN",
        "MATERIAL_REPORT_UNLINKED",
        "MATERIAL_LINK_UNRESOLVED",
    }
)


def actionable_input_issues(items: list[dict[str, str]]) -> list[dict[str, str]]:
    """待同步状态和零金额排除说明不触发缺数提醒，原始计数保留在运行记录。"""
    return [
        item
        for item in items
        if item.get("reason")
        not in SYNC_PENDING_REASONS | {"ZERO_VALUE_UNLINKED_POSTS_IGNORED"}
    ]
