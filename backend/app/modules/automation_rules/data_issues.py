"""区分正常待数据与需要提醒的数据异常，原始问题仍保留在运行记录。"""


def actionable_input_issues(items: list[dict[str, str]]) -> list[dict[str, str]]:
    """首次投放报表尚未出现是待数据状态；无命中时不额外触发消息轰炸。"""
    return [item for item in items if item.get("reason") != "MATERIAL_LINK_MISSING"]
