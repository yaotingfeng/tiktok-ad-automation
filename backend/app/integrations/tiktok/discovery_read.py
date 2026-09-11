"""两通道共用分阶段规范化；每次读取只返回最多50条目录或有界完整授权集合。"""

from app.integrations.tiktok.contracts.accounts import (
    AdvertiserFact,
    BusinessCenterFact,
    DirectoryPage,
)
from app.integrations.tiktok.contracts.discovery import (
    AdvertiserDetails,
    AuthorizedAdvertisers,
)
from app.integrations.tiktok.read_normalization import AccountsReadAdapter


class DiscoveryReadAdapter(AccountsReadAdapter):
    def discovery_business_centers(
        self, *, page: int, page_size: int
    ) -> DirectoryPage[BusinessCenterFact]:
        # 独立发现协议只为完整性核验使用；普通 runtime.business_centers 仍拒绝扩张目录。
        from app.integrations.tiktok import accounts as api

        if page_size != 50:
            raise api.schema_error()
        return self._business_centers(page=page, page_size=page_size)

    def authorized_advertisers(self) -> AuthorizedAdvertisers:
        from app.integrations.tiktok import accounts as api
        from app.integrations.tiktok.contracts.discovery import (
            AUTHORIZED_LIST_SOURCE,
            MAX_AUTHORIZED_ADVERTISERS,
            AuthorizedAdvertisers,
        )
        from app.integrations.tiktok.read_normalization import object_data

        response = self._call("accounts.list_authorized_advertisers", {})
        data = object_data(response)
        # 已核实的无分页读取只接受完整 list，任何额外分页/截断结构都要求重新核实。
        if set(data) != {"list"}:
            raise api.schema_error()
        rows = api.object_list(data)
        if len(rows) > MAX_AUTHORIZED_ADVERTISERS:
            raise api.schema_error()
        ids = tuple(api.external_id(row, "advertiser_id") for row in rows)
        try:
            return AuthorizedAdvertisers(
                advertiser_ids=ids,
                evidence=response.evidence,
                completeness_source=AUTHORIZED_LIST_SOURCE,
            )
        except ValueError:
            raise api.schema_error() from None

    def assets(
        self, *, bc_id: str, page: int, page_size: int
    ) -> DirectoryPage[AdvertiserFact]:
        from app.integrations.tiktok import accounts as api
        from app.integrations.tiktok.contracts.accounts import AdvertiserFact
        from app.integrations.tiktok.contracts.discovery import DISCOVERY_PAGE_SIZE
        from app.integrations.tiktok.read_normalization import directory_page

        if page_size != DISCOVERY_PAGE_SIZE:
            raise api.schema_error()
        response, data = self._assets(bc_id, page, page_size)
        rows = tuple(
            AdvertiserFact(
                **api.asset_row(row),
                currency="",
                timezone="",
                remote_status="UNKNOWN",
                authorized=None,
            )
            for row in data["list"]
        )
        return directory_page(
            data, items=rows, page=page, page_size=page_size, evidence=response.evidence
        )

    def advertiser_details(
        self, *, advertiser_ids: tuple[str, ...]
    ) -> AdvertiserDetails:
        from app.integrations.tiktok import accounts as api
        from app.integrations.tiktok.contracts.accounts import (
            AdvertiserFact,
            require_id,
        )
        from app.integrations.tiktok.contracts.discovery import (
            DISCOVERY_PAGE_SIZE,
            AdvertiserDetails,
        )
        from app.integrations.tiktok.read_normalization import object_data

        try:
            if (
                type(advertiser_ids) is not tuple
                or not 1 <= len(advertiser_ids) <= DISCOVERY_PAGE_SIZE
                or len(set(advertiser_ids)) != len(advertiser_ids)
            ):
                raise ValueError("invalid advertiser batch")
            for identity in advertiser_ids:
                require_id(identity)
        except ValueError:
            raise api.schema_error() from None
        response = self._call(
            "accounts.get_advertisers", {"advertiser_ids": list(advertiser_ids)}
        )
        data = object_data(response)
        rows = api.object_list(data)
        if len(rows) != len(advertiser_ids) or {
            api.external_id(row, "advertiser_id") for row in rows
        } != set(advertiser_ids):
            raise api.schema_error()
        return AdvertiserDetails(
            items=tuple(
                AdvertiserFact(**api.detail_row(row), authorized=True) for row in rows
            ),
            evidence=response.evidence,
        )
