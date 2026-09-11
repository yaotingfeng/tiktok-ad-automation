"""数据库唯一约束保护的短随机批次号；不提交调用方事务。"""

import secrets

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.core.errors import DomainError
from app.modules.builds.preview_models import BuildPreview

BATCH_NUMBER_LENGTH = 4
BATCH_NUMBER_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
BATCH_NUMBER_ATTEMPTS = 10


def random_batch_number() -> str:
    # 4 位大写字母/数字共 36**4 种组合，唯一性仍由数据库裁决。
    return "".join(
        secrets.choice(BATCH_NUMBER_ALPHABET) for _ in range(BATCH_NUMBER_LENGTH)
    )


def is_current_batch_number(value: str) -> bool:
    return len(value) == BATCH_NUMBER_LENGTH and all(
        char in BATCH_NUMBER_ALPHABET for char in value
    )


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
