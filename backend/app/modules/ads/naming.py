"""系列名固定取前两段；备注不参与剧归组。"""

import unicodedata
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class CampaignIdentity:
    provider_label: str | None
    drama_name: str | None
    status: Literal["VALID", "INVALID"]

    def __post_init__(self) -> None:
        if self.status not in ("VALID", "INVALID"):
            raise ValueError("invalid campaign identity status")
        for value in (self.provider_label, self.drama_name):
            if value is not None and (type(value) is not str or not value.strip()):
                raise ValueError("invalid campaign identity segment")
        if self.status == "VALID" and (
            self.provider_label is None or self.drama_name is None
        ):
            raise ValueError("valid campaign identity requires both segments")


def parse_campaign_name(name: str) -> CampaignIdentity:
    if type(name) is not str:
        raise ValueError("campaign name must be a string")
    # 保留空段：嘉书--备注必须判为无效，不能将备注前移充当剧名。
    parts = name.split("-", 2)
    if len(parts) < 2:
        return CampaignIdentity(None, None, "INVALID")
    provider, drama = (unicodedata.normalize("NFC", part.strip()) for part in parts[:2])
    if not provider or not drama:
        return CampaignIdentity(None, None, "INVALID")
    return CampaignIdentity(provider, drama, "VALID")
