"""广告管理权限规则：verify_route、usable_grants 与管理预览共用，不依赖其他业务模块。"""

from typing import Protocol


class _Grant(Protocol):
    can_build: bool
    can_upload: bool


# 各管理操作可作用的对象层级；平台接口按层级独立，素材开关只作用于广告内素材。
MANAGEMENT_ENTITY_KINDS: dict[str, frozenset[str]] = {
    "set_status": frozenset({"campaign", "adgroup", "ad"}),
    "update_budget": frozenset({"campaign", "adgroup"}),
    "update_roas": frozenset({"adgroup"}),
    "set_material_status": frozenset({"ad"}),
}
CREATIVE_SCOPES = frozenset({"6", "61", "611"})


def management_allowed(
    grant: _Grant, scopes: list[str] | tuple[str, ...], operation: str
) -> bool:
    """广告管理权限与搭建权限同一依据：账户同步已核实 ADMIN/OPERATOR 角色且授权含
    广告管理（官方 scope 2 / MCP mcp:tt4b）时 can_build 为真。素材开关还要求创意管理：
    官方连接看 6/61/611，MCP 连接以已核实的上传权限为准。"""
    if not grant.can_build:
        return False
    if operation != "set_material_status":
        return True
    return bool(set(scopes) & CREATIVE_SCOPES) or (
        "mcp:tt4b" in scopes and grant.can_upload
    )
