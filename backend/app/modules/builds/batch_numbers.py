"""数据库唯一约束保护的短随机批次号；不提交调用方事务。"""

import secrets

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.core.errors import DomainError
from app.modules.builds.preview_models import BuildPreview

BATCH_NUMBER_DIGITS = 12
BATCH_NUMBER_ATTEMPTS = 10


def random_batch_number() -> str:
    return f"{secrets.randbelow(10**BATCH_NUMBER_DIGITS):0{BATCH_NUMBER_DIGITS}d}"


def insert_preview_with_number(session: Session, row: BuildPreview) -> None:
    for _ in range(BATCH_NUMBER_ATTEMPTS):
        row.batch_short_id = random_batch_number()
        try:
            # 不以先查后写判断唯一性；并发碰撞由全库唯一索引裁决。
            with session.begin_nested():
                session.add(row)
                session.flush()
            return
        except IntegrityError as error:
            constraint = getattr(
                getattr(error.orig, "diag", None), "constraint_name", None
            )
            if constraint != "uq_preview_batch_short":
                raise
    raise DomainError(
        "preview_batch_number_exhausted", "暂未分配到唯一随机号，请重试", retryable=True
    )
