"""领取时重新签发原时长的短期执行授权，排队不消耗模型分析窗口。"""

from datetime import datetime, timedelta
from math import ceil


def claim_expiry(
    *,
    created_at: datetime,
    expires_at: datetime,
    restart_generation: int,
    now: datetime,
) -> datetime:
    # 首次保留用户请求的 30..900 秒；恢复轮沿用既有 restart_task 的五分钟。
    seconds = (
        300
        if restart_generation
        else max(30, min(900, ceil((expires_at - created_at).total_seconds())))
    )
    return now + timedelta(seconds=seconds)
