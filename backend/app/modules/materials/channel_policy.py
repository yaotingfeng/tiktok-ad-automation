"""本地固定合同的 URL 上传门禁；不从工具存在或请求值推断服务能力。"""

from dataclasses import dataclass

from app.core.config import settings
from app.core.errors import DomainError


@dataclass(frozen=True)
class MaterialUploadPolicy:
    max_bytes: int | None
    identity_verified: bool
    no_auto_fix_verified: bool
    no_auto_bind_verified: bool
    unsafe_server_retry: bool


def require_url_upload(policy: MaterialUploadPolicy, *, byte_size: int) -> None:
    # 1/字符串等真值不能替代经过核实的布尔事实，未知服务上限不能套用应用容量。
    if (
        type(policy.max_bytes) is not int
        or policy.max_bytes <= 0
        or policy.identity_verified is not True
        or policy.no_auto_fix_verified is not True
        or policy.no_auto_bind_verified is not True
        or policy.unsafe_server_retry is not False
    ):
        raise DomainError("material_channel_unverified", "当前连接的素材入库能力待核实")
    if type(byte_size) is not int or not 0 < byte_size <= min(
        settings.MATERIAL_URL_MAX_UPLOAD_BYTES, policy.max_bytes
    ):
        raise DomainError(
            "material_channel_capacity", "文件超过当前连接已核实的上传容量"
        )
