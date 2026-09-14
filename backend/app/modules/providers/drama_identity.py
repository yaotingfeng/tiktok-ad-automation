"""展示编号与调用接口的技术标识分离，不推测网眼数字编号。"""

from typing import Any


def display_id(
    kind: str,
    external: str | None,
    stored: str | None = None,
    attribution: dict[str, Any] | None = None,
) -> str | None:
    if stored:
        return stored
    if kind == "wangyan":
        value = (attribution or {}).get("drama_int_id")
        if type(value) is int and value > 0:
            return str(value)
        if (
            isinstance(value, str)
            and value.isascii()
            and value.isdigit()
            and int(value) > 0
        ):
            return value
        return None
    return external if external and not external.startswith("LOCAL-") else None
