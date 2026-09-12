"""URL 上传的本地容量限制；内容身份由上传后的实际回读核实。"""

from dataclasses import dataclass

from app.core.config import settings
from app.core.errors import DomainError


@dataclass(frozen=True)
class MaterialUploadPolicy:
    # 这是应用工程上限，不声明官方容量或服务内部重试保证。
    max_bytes: int | None


def require_url_upload(policy: MaterialUploadPolicy, *, byte_size: int) -> None:
    # 未接入的接口版本仍拒绝发送，避免将当前合同用于任意未来版本。
    if type(policy.max_bytes) is not int or policy.max_bytes <= 0:
        raise DomainError("material_channel_unverified", "当前素材接口版本尚未接入")
    if type(byte_size) is not int or not 0 < byte_size <= min(
        settings.MATERIAL_URL_MAX_UPLOAD_BYTES, policy.max_bytes
    ):
        raise DomainError(
            "material_channel_capacity", "文件超过当前连接的上传容量限制"
        )
