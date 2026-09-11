"""持久发现阶段的只读合同；与普通分页账户网关独立。"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .accounts import (
    AccountRoleFact,
    AdvertiserFact,
    AuthorizationFacts,
    BusinessCenterFact,
    DirectoryPage,
    require_id,
)
from .common import CallEvidence

# 官方文档与固定 MCP 工具均未提供分页参数；这是完整响应而非伪造远端末页。
AUTHORIZED_LIST_SOURCE = (
    "https://business-api.tiktok.com/portal/docs?id=1738455508553729"
)
MAX_AUTHORIZED_ADVERTISERS = 100_000
DISCOVERY_PAGE_SIZE = 50


@dataclass(frozen=True)
class ObservedAuthorization:
    facts: AuthorizationFacts
    evidence: CallEvidence


@dataclass(frozen=True)
class AuthorizedAdvertisers:
    advertiser_ids: tuple[str, ...]
    evidence: CallEvidence
    completeness_source: str

    def __post_init__(self) -> None:
        if (
            type(self.advertiser_ids) is not tuple
            or len(self.advertiser_ids) > MAX_AUTHORIZED_ADVERTISERS
            or len(self.advertiser_ids) != len(set(self.advertiser_ids))
            or self.completeness_source != AUTHORIZED_LIST_SOURCE
        ):
            raise ValueError("unverified complete advertiser response")
        for identity in self.advertiser_ids:
            require_id(identity)


@dataclass(frozen=True)
class AdvertiserDetails:
    items: tuple[AdvertiserFact, ...]
    evidence: CallEvidence

    def __post_init__(self) -> None:
        if type(self.items) is not tuple or len(self.items) > DISCOVERY_PAGE_SIZE:
            raise ValueError("unbounded advertiser detail batch")
        if len({item.advertiser_id for item in self.items}) != len(self.items):
            raise ValueError("duplicate advertiser details")


@runtime_checkable
class DiscoveryAccountsGateway(Protocol):
    def discovery_business_centers(
        self, *, page: int, page_size: int
    ) -> DirectoryPage[BusinessCenterFact]: ...
    def business_centers(
        self, *, page: int, page_size: int
    ) -> DirectoryPage[BusinessCenterFact]: ...
    def roles(
        self, *, bc_id: str, page: int, page_size: int
    ) -> DirectoryPage[AccountRoleFact]: ...
    def observe_authorization(self) -> ObservedAuthorization: ...
    def authorized_advertisers(self) -> AuthorizedAdvertisers: ...
    def assets(
        self, *, bc_id: str, page: int, page_size: int
    ) -> DirectoryPage[AdvertiserFact]: ...
    def advertiser_details(
        self, *, advertiser_ids: tuple[str, ...]
    ) -> AdvertiserDetails: ...
