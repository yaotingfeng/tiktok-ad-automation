"""代码审查拥有的上传合同记录；不接受用户配置、工具存在或应用容量作为证据。"""

from dataclasses import dataclass
from typing import Literal

from app.modules.materials.channel_policy import MaterialUploadPolicy

UNKNOWN_UPLOAD_POLICY = MaterialUploadPolicy(None, False, False, False, True)


@dataclass(frozen=True)
class MaterialUploadEvidence:
    channel: str
    adapter_contract_revision: str
    policy: MaterialUploadPolicy
    category: Literal["DOCUMENTED", "UNVERIFIED", "SYNTHETIC"]
    sources: tuple[str, ...]
    notes: str


UPLOAD_EVIDENCE = (
    MaterialUploadEvidence(
        channel="OFFICIAL_API",
        adapter_contract_revision="official-api-v1",
        policy=MaterialUploadPolicy(None, False, True, True, True),
        category="UNVERIFIED",
        sources=(
            "https://github.com/tiktok/tiktok-business-api-sdk/blob/main/js_sdk/docs/AdUploadBody.md",
            "https://www.postman.com/tiktok/tiktok-api-for-business/request/999yihe/file-video-upload",
            "docs/superpowers/specs/2026-09-10-r2-transient-video-upload-design.md#2026-09-11-单文件容量调整",
        ),
        notes=(
            "2026-09-12核对：两个false行为有官方文档；URL服务容量和服务重试未知。"
            "SDK发送video_signature不证明URL已完成内容比对。API沿既有已授权路径使用"
            "应用容量、零客户端重试与实际MD5/大小回读，不套用新增MCP服务能力门禁。"
        ),
    ),
    MaterialUploadEvidence(
        channel="OFFICIAL_MCP",
        adapter_contract_revision="9ac2576299ae5d447fe9df2d4ba26180ca9bef76a56f99601c0cdd5b5d7dbc01",
        policy=UNKNOWN_UPLOAD_POLICY,
        category="UNVERIFIED",
        sources=(
            "app/integrations/tiktok/mcp/protocol-profile.json",
            "app/integrations/tiktok/mcp/tool-contracts.json",
        ),
        notes=(
            "P0公开工具声明不是当前连接或服务能力保证：容量、不可变内容身份、"
            "服务重试均未核实。两个字段可传false也不足以开放上传。"
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
