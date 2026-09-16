"""可信内容身份统一用于租户候选去重与 BC 转存幂等，不修改上传历史。"""

import re

from sqlalchemy import String, and_, case, cast, literal
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import col

from .models import MaterialFile


def content_key(material: MaterialFile) -> str:
    if (
        material.digest_verified_at is not None
        and re.fullmatch(r"[0-9a-f]{64}", material.sha256 or "")
        and re.fullmatch(r"[0-9a-f]{32}", material.video_md5 or "")
    ):
        return f"sha256:{material.sha256}:{material.video_md5}:{material.byte_size}"
    return f"material:{material.id}"


def material_content_key_expression() -> ColumnElement[str]:
    """必须与 Python 键相同；未知/无效摘要按独立文件保留，避免错误合并。"""
    verified = and_(
        col(MaterialFile.digest_verified_at).is_not(None),
        col(MaterialFile.sha256).op("~")(r"^[0-9a-f]{64}$"),
        col(MaterialFile.video_md5).op("~")(r"^[0-9a-f]{32}$"),
    )
    return case(
        (
            verified,
            literal("sha256:")
            + col(MaterialFile.sha256)
            + ":"
            + col(MaterialFile.video_md5)
            + ":"
            + cast(MaterialFile.byte_size, String),
        ),
        else_=literal("material:") + cast(MaterialFile.id, String),
    )
