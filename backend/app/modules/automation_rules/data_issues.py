"""区分正常待数据与需要提醒的数据异常，原始问题仍保留在运行记录。"""

# 以下原因都是实时目录/素材关联尚未追平的待同步状态：首次投放报表未出现、上级目录未发布、
# 报表帖子或历史帖子尚未定位。它们按两分钟周期自然收敛，数量每轮波动，无命中时逐轮推飞书
# 只会刷屏；对应素材仍不参与判断（不会误停），明细继续保留在运行记录与页面。
# 报表过期（STALE_REPORT）等真正的同步故障仍需提醒。
SYNC_PENDING_REASONS = frozenset(
    {
        "MATERIAL_LINK_MISSING",
        "MATERIAL_PARENT_UNKNOWN",
        "MATERIAL_REPORT_UNLINKED",
        "MATERIAL_LINK_UNRESOLVED",
    }
)


def actionable_input_issues(items: list[dict[str, str]]) -> list[dict[str, str]]:
    """待同步状态不触发无命中提醒，避免消息轰炸。"""
    return [item for item in items if item.get("reason") not in SYNC_PENDING_REASONS]
