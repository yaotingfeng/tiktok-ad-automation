"""官方 OAuth 数字 scope 的唯一解释处，业务模块只消费 AuthorizationFacts。"""

import json
from datetime import datetime

from app.integrations.tiktok.contracts.accounts import AuthorizationFacts

ISSUER = "https://business-api.tiktok.com"
RESOURCE = "https://business-api.tiktok.com/open_api/v1.3"
CONTRACT_REVISION = "official-api-v1"


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
    # 已有官方权限合同 doc1753986142651394；此处不以目录可见性授予写入。
    return AuthorizationFacts(
        subject_id=None,
        grant_id=None,
        issuer=ISSUER,
        resource=RESOURCE,
        scopes=tuple(str(v) for v in sorted(scopes)),
        read_authorized=None,
        build_authorized=(2 in scopes) if known else None,
        upload_authorized=bool(scopes & {6, 61, 611}) if known else None,
        evidence_source="OFFICIAL_TOKEN_SCOPE",
        observed_at=observed_at,
    )
