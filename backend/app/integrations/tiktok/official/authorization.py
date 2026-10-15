"""官方 OAuth 数字 scope 的唯一解释处，业务模块只消费 AuthorizationFacts。"""

import json
from datetime import datetime

from app.integrations.tiktok.contracts.accounts import AuthorizationFacts
from app.integrations.tiktok.contracts.management import MANAGEMENT_OPERATIONS

ISSUER = "https://business-api.tiktok.com"
RESOURCE = "https://business-api.tiktok.com/open_api/v1.3"
CONTRACT_REVISION = "official-api-v1"
# 官方权限层级（doc 1753986142651394）：父级 2 为广告管理，覆盖系列、广告组、广告的
# 状态、预算与出价；6 / 61 / 611 为创意与视频管理。素材开关同时属于两者，需同时具备。
_ADS_MANAGEMENT = 2
_CREATIVE_MANAGEMENT = frozenset({6, 61, 611})


def management_operations_for_scopes(scopes: set[int]) -> frozenset[str]:
    """官方 scope 到管理操作的唯一映射；实际写权还需账户角色为 ADMIN/OPERATOR。"""
    if _ADS_MANAGEMENT not in scopes:
        return frozenset()
    operations = {"set_status", "update_budget", "update_roas"}
    if scopes & _CREATIVE_MANAGEMENT:
        operations.add("set_material_status")
    return frozenset(operations & MANAGEMENT_OPERATIONS)


def material_authorization(
    material: dict[str, str], *, observed_at: datetime
) -> AuthorizationFacts:
    scopes: set[int]
    try:
        values = json.loads(material["scope"])
        if type(values) is not list or len(values) > 1024:
            raise ValueError("invalid scope")
        scopes = set()
        for value in values:
            if type(value) is not int or not 0 < value < 2**64:
                raise ValueError("invalid scope")
            scopes.add(value)
        known = True
    except KeyError, ValueError, TypeError:
        scopes, known = set(), False
    # 管理操作只按官方 scope 层级映射，不以目录可见性或授权材料里的自报字段授予写入。
    management = management_operations_for_scopes(scopes) if known else frozenset()
    return AuthorizationFacts(
        subject_id=None,
        grant_id=None,
        issuer=ISSUER,
        resource=RESOURCE,
        scopes=tuple(str(v) for v in sorted(scopes)),
        read_authorized=None,
        build_authorized=(2 in scopes) if known else None,
        management_operations=management,
        upload_authorized=bool(scopes & {6, 61, 611}) if known else None,
        evidence_source="OFFICIAL_TOKEN_SCOPE",
        observed_at=observed_at,
    )
