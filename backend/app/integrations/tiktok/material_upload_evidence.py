"""已接入的 URL 上传版本与工程容量；不声明未获证明的服务内部行为。"""

from dataclasses import dataclass
from typing import Literal

from app.modules.materials.channel_policy import MaterialUploadPolicy

UNKNOWN_UPLOAD_POLICY = MaterialUploadPolicy(None)


@dataclass(frozen=True)
class MaterialUploadEvidence:
    channel: str
    adapter_contract_revision: str
    policy: MaterialUploadPolicy
    category: Literal["APPLICATION", "SYNTHETIC"]
    sources: tuple[str, ...]
    notes: str


UPLOAD_EVIDENCE = (
    MaterialUploadEvidence(
        channel="OFFICIAL_API",
        adapter_contract_revision="official-api-v1",
        policy=MaterialUploadPolicy(1024**3),
        category="APPLICATION",
        sources=(
            "https://github.com/tiktok/tiktok-business-api-sdk/blob/main/js_sdk/docs/AdUploadBody.md",
            "https://www.postman.com/tiktok/tiktok-api-for-business/request/999yihe/file-video-upload",
            "docs/superpowers/specs/2026-09-10-r2-transient-video-upload-design.md#2026-09-11-单文件容量调整",
        ),
        notes=(
            "应用容量、零客户端重试与实际MD5/大小回读；不声明服务内部保证。"
        ),
    ),
    MaterialUploadEvidence(
        channel="OFFICIAL_MCP",
        adapter_contract_revision="c8329fde77d294d3c9bf39fca05c1e640e327928b827aba38fd10321f8c44965",
        policy=MaterialUploadPolicy(1024**3),
        category="APPLICATION",
        sources=(
            "app/integrations/tiktok/mcp/protocol-profile.json",
            "app/integrations/tiktok/mcp/tool-contracts.json",
        ),
        notes=(
            "2026-09-13用户确认URL上传已实际使用并要求接通服务器流程。"
            "按应用容量发送一次，关闭自动修复和绑定，保留原件并实际回读MD5/大小。"
            "结果未知只回查；账户授权和角色仍独立验证，不从工具可见性推断。"
        ),
    ),
)


def material_upload_policy(
    *, channel: str, adapter_contract_revision: str
) -> MaterialUploadPolicy:
    matches = [
        row
        for row in UPLOAD_EVIDENCE
        if row.channel == channel
        and row.adapter_contract_revision == adapter_contract_revision
    ]
    # 未知/重复版本都不能择一放行；合成记录仅由测试局部替换此不可变代码目录。
    return matches[0].policy if len(matches) == 1 else UNKNOWN_UPLOAD_POLICY
