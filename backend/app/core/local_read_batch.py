"""仅本地预览显式启用的短批次权限复用，退出前重新核验，不用于外部写操作。"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from time import monotonic
from typing import Any, cast

from sqlmodel import Session

from app.core.errors import DomainError


@dataclass
class _Batch:
    session: Session
    transaction: Any
    nested: Any
    started: float
    # 只登记成功的纯权限读取；失败不缓存。大小/时间均受限，不跨事务保留。
    checks: dict[
        Any, tuple[Callable[..., Any], tuple[Any, ...], dict[str, Any], Any]
    ] = field(default_factory=dict)


_current: ContextVar[_Batch | None] = ContextVar("local_preview_reads", default=None)


def local_read_batch_active(session: Session) -> bool:
    batch = _current.get()
    return bool(
        batch is not None
        and session is batch.session
        and session.get_transaction() is batch.transaction
        and session.get_nested_transaction() is batch.nested
        and monotonic() - batch.started < 3
    )


def reuse_local_read[F: Callable[..., Any]](function: F) -> F:
    """限定装饰纯权限函数；默认行为完全不变，唯一本地消费者主动打开范围。"""

    @wraps(function)
    def checked(session: Session, *args: Any, **kwargs: Any) -> Any:
        batch = _current.get()
        if batch is None or not local_read_batch_active(session):
            return function(session, *args, **kwargs)
        key = (function, args, tuple(sorted(kwargs.items())))
        if key in batch.checks:
            return batch.checks[key][3]
        value = function(session, *args, **kwargs)
        if len(batch.checks) < 1024:
            batch.checks[key] = (function, args, kwargs, value)
        return value

    return cast(F, checked)


@contextmanager
def local_read_batch(session: Session) -> Iterator[None]:
    """有界计算期间复用；无论成功/异常均清理，成功退出前逐个重验原授权。"""
    if _current.get() is not None or session.get_transaction() is None:
        raise RuntimeError("Local read batch requires a non-nested active scope")
    batch = _Batch(
        session,
        session.get_transaction(),
        session.get_nested_transaction(),
        monotonic(),
    )
    token = _current.set(batch)
    try:
        yield
    finally:
        _current.reset(token)
    # 异常退出不会到此处；调用者回滚其保存点。重验不使用计算阶段的事实。
    if (
        session.get_transaction() is not batch.transaction
        or session.get_nested_transaction() is not batch.nested
    ):
        raise RuntimeError("Local read batch cannot cross transaction boundaries")
    # 新建完全空的验证范围；不同顶层检查的共同依赖只读一次最新值，避免
    # 场景→账户→连接→租户在最终复核时再次产生同样的乘法查询。
    fresh = _Batch(session, batch.transaction, batch.nested, monotonic())
    token = _current.set(fresh)
    try:
        # 冻结标记与最终 JSON 必须由调用者一次 flush，不能被读取提前刷入。
        with session.no_autoflush:
            for function, args, kwargs, previous in batch.checks.values():
                if reuse_local_read(function)(session, *args, **kwargs) != previous:
                    raise DomainError(
                        "account_authorization_changed",
                        "账户授权已改变，请重新准备草稿",
                    )
    finally:
        _current.reset(token)
