"""平台文件名保留业务编号；内部关联只使用短后缀，不以 UUID 替换原名。"""

import re
from pathlib import PurePath


def video_file_name(original: str, *, correlation: str | None = None) -> str:
    name = re.sub(r"[\x00-\x1f\x7f/\\]", "_", original).strip() or "video.mp4"
    extension = PurePath(name).suffix
    if extension.lower() not in {
        ".mp4",
        ".mov",
        ".m4v",
        ".avi",
        ".webm",
        ".mpeg",
        ".3gp",
    }:
        extension = ".mp4"
        stem = name
    else:
        stem = name[: -len(extension)] or "video"
    suffix = (f"-{correlation}" if correlation else "") + extension
    available = 100 - len(suffix.encode("utf-8"))
    if len(stem.encode("utf-8")) > available:
        # 长名称从中间压缩，保留开头剧名和末尾版本/集数，不截掉业务编号。
        head = (available - 3) // 2
        tail = available - 3 - head
        stem = (
            stem.encode()[:head].decode(errors="ignore")
            + "..."
            + stem.encode()[-tail:].decode(errors="ignore")
        )
    return stem + suffix
