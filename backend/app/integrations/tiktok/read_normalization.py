"""共享纯读取规范化；远端原始 JSON 只在适配器内部存在。"""

from abc import ABC, abstractmethod
from hashlib import sha256
from typing import Any

from pydantic import ValidationError

from app.core.errors import DomainError
from app.integrations.tiktok.sdk import checked_data
from app.modules.builds.scene_schemas import SceneResource

from . import accounts as legacy_accounts
from .contracts.accounts import (
    AccountRoleFact,
    AdvertiserFact,
    AuthorizationFacts,
    BusinessCenterFact,
    CandidateReadContext,
    DirectoryPage,
    RuntimeReadContext,
    require_id,
    require_page,
)
from .contracts.common import CallEvidence, McpBusinessResponse
from .contracts.scenes import FACT_TYPES, ScenePage

PAGE_SIZE = 50


def _invalid() -> DomainError:
    return DomainError("scene_response_unverified", "场景返回缺少可核实信息")


def _string(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 255:
        raise _invalid()
    return value


def parse_page(
    response: object,
    *,
    resource: SceneResource,
    page: int,
    advertiser_id: str,
    bc_id: str,
    minis_id: str | None,
) -> tuple[dict[str, Any], bool, str | None]:
    data = checked_data(response)
    raw_request_id = (
        response.get("request_id")
        if isinstance(response, dict)
        else getattr(response, "request_id", None)
    )
    request_id = (
        raw_request_id
        if isinstance(raw_request_id, str) and len(raw_request_id) <= 128
        else None
    )
    if resource == "regions":
        codes, rows = data.get("region_list"), data.get("region_info")
        if (
            page != 1
            or not isinstance(codes, list)
            or not isinstance(rows, list)
            or len(codes) > 300
            or len(rows) > 300
            or any(
                not isinstance(value, str)
                or len(value) != 2
                or not value.isascii()
                or not value.isupper()
                or not value.isalpha()
                for value in codes
            )
            or len(set(codes)) != len(codes)
        ):
            raise _invalid()
        locations: dict[str, str] = {}
        identifiers: set[str] = set()
        for row in rows:
            if (
                not isinstance(row, dict)
                or row.get("level") != "COUNTRY"
                or row.get("area_type") != "ADMIN"
            ):
                raise _invalid()
            code, identity = (
                _string(row.get("region_code")),
                _string(row.get("location_id")),
            )
            if (
                code not in codes
                or code in locations
                or identity in identifiers
                or identity == code
            ):
                raise _invalid()
            locations[code] = identity
            identifiers.add(identity)
        if set(locations) != set(codes):
            raise _invalid()
        return (
            {
                "locations": [
                    {"region_code": code, "location_id": locations[code]}
                    for code in sorted(locations)
                ]
            },
            True,
            request_id,
        )
    if resource == "cta":
        values = data.get("recommend_assets")
        if not isinstance(values, list) or len(values) > 50:
            raise _invalid()
        ids = set()
        assets = []
        for item in values:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("asset_ids"), list)
                or not 1 <= len(item["asset_ids"]) <= 50
            ):
                raise _invalid()
            for value in item["asset_ids"]:
                ids.add(_string(value))
                if len(ids) > 50:
                    raise _invalid()
            assets.append(
                {
                    "asset_ids": sorted(set(item["asset_ids"])),
                    "asset_content": _string(item.get("asset_content")),
                }
            )
        if len(ids) > 50:
            raise _invalid()
        return {"asset_ids": sorted(ids), "recommend_assets": assets}, True, request_id
    if resource == "vbo":
        result: dict[str, Any] = {}
        for key in ("vo_status", "vo_min_roas", "roas_status_day0", "roas_status_day7"):
            if key in data:
                result[key] = _string(data[key])
        if not result:
            raise _invalid()
        return result, True, request_id
    values, info = (
        data.get("identity_list" if resource == "identity" else "list"),
        data.get("page_info"),
    )
    if (
        not isinstance(values, list)
        or len(values) > PAGE_SIZE
        or not isinstance(info, dict)
    ):
        raise _invalid()
    if (
        any(
            type(info.get(key)) is not int
            for key in ("page", "page_size", "total_page", "total_number")
        )
        or info["page"] != page
        or info["page_size"] != PAGE_SIZE
        or not 0 <= info["total_page"] <= 1000
        or info["total_number"] < 0
    ):
        raise _invalid()
    last = page >= max(1, info["total_page"])
    if page > max(1, info["total_page"]) or not last and not values:
        raise _invalid()
    matches = []
    id_hashes: set[str] = set()
    id_field = {
        "account_roles": "asset_id",
        "minis": "minis_id",
        "identity": "identity_id",
    }[resource]
    for item in values:
        if not isinstance(item, dict):
            raise _invalid()
        remote_id = _string(item.get(id_field))
        digest = sha256(remote_id.encode()).hexdigest()
        if digest in id_hashes:
            raise _invalid()
        id_hashes.add(digest)
        if resource == "account_roles":
            if remote_id == advertiser_id:
                if item.get("asset_type") != "ADVERTISER" or item.get(
                    "advertiser_role"
                ) not in {"ADMIN", "OPERATOR", "ANALYST"}:
                    raise _invalid()
                matches.append(
                    {"advertiser_id": remote_id, "role": item["advertiser_role"]}
                )
        elif resource == "minis":
            if remote_id == minis_id:
                regions = item.get("region_codes")
                if (
                    not isinstance(regions, list)
                    or not 1 <= len(regions) <= 300
                    or any(
                        not isinstance(region, str)
                        or len(region) != 2
                        or not region.isascii()
                        or not all("A" <= letter <= "Z" for letter in region)
                        for region in regions
                    )
                ):
                    raise _invalid()
                if item.get("minis_status") not in {"ACTIVE", "INACTIVE"} or item.get(
                    "minis_type"
                ) not in {"MINI_SERIES", "MINI_GAME"}:
                    raise _invalid()
                matches.append(
                    {
                        "minis_id": remote_id,
                        "status": item["minis_status"],
                        "type": item["minis_type"],
                        "regions": sorted(set(regions)),
                    }
                )
        else:
            if (
                item.get("identity_type") != "BC_AUTH_TT"
                or item.get("identity_authorized_bc_id") != bc_id
            ):
                raise _invalid()
            if (
                item.get("available_status") == "AVAILABLE"
                and item.get("can_push_video") is True
                and item.get("is_gpppa") is False
            ):
                matches.append(
                    {
                        "identity_id": remote_id,
                        "identity_type": "BC_AUTH_TT",
                        "identity_authorized_bc_id": bc_id,
                    }
                )
    return (
        {
            "matches": matches[:2],
            "item_id_hashes": sorted(id_hashes),
            "total_number": info["total_number"],
            "total_page": info["total_page"],
            "seen": len(values),
        },
        last,
        request_id,
    )


def scene_page(
    data: dict[str, Any] | list[dict[str, Any]],
    *,
    evidence: CallEvidence,
    resource: SceneResource,
    page: int,
    advertiser_id: str,
    bc_id: str,
    minis_id: str | None,
) -> ScenePage:
    facts, last, _ = parse_page(
        {"data": data, "request_id": evidence.request_id},
        resource=resource,
        page=page,
        advertiser_id=advertiser_id,
        bc_id=bc_id,
        minis_id=minis_id,
    )
    try:
        return ScenePage(
            resource=resource,
            page=page,
            last=last,
            facts=FACT_TYPES[resource].model_validate(facts),
            evidence=evidence,
        )
    except ValidationError:
        raise _invalid() from None


def scene_arguments(
    *,
    resource: SceneResource,
    advertiser_id: str,
    bc_id: str,
    page: int,
    minis_id: str | None,
) -> tuple[str, dict[str, Any]]:
    try:
        require_id(advertiser_id)
        require_id(bc_id)
        require_page(page, PAGE_SIZE)
        # 账户目录允许更大容量；场景分页合同仍固定最多 1000 页，发送前拒绝越界。
        if page > 1000:
            raise ValueError("scene page out of bounds")
        if minis_id is not None:
            require_id(minis_id)
    except ValueError:
        raise DomainError("scene_request_invalid", "场景读取参数无效") from None
    if resource not in FACT_TYPES or (
        resource in ("cta", "vbo", "regions") and page != 1
    ):
        raise DomainError("scene_request_invalid", "场景读取参数无效")
    if resource == "account_roles":
        return "accounts.list_bc_assets", {
            "bc_id": bc_id,
            "asset_type": "ADVERTISER",
            "page": page,
            "page_size": PAGE_SIZE,
        }
    args: dict[str, Any] = {"advertiser_id": advertiser_id}
    if resource in ("identity", "minis"):
        args.update(page=page, page_size=PAGE_SIZE)
        if resource == "identity":
            args.update(identity_type="BC_AUTH_TT", identity_authorized_bc_id=bc_id)
        return (
            "scene.list_identities" if resource == "identity" else "scene.list_minis"
        ), args
    args.update(
        objective_type="APP_PROMOTION",
        promotion_type="MINI_APP",
        placements=["PLACEMENT_TIKTOK"],
    )
    if resource == "cta":
        args.update(new_version=True, optimization_goal="VALUE")
        return "scene.recommend_ctas", args
    args["app_promotion_type"] = "MINIS"
    if resource == "regions":
        args.update(level_range="TO_COUNTRY", language="en")
        return "scene.list_regions", args
    args.update(campaign_automation_type="UPGRADED_SMART_PLUS", budget_optimize_on=True)
    return "scene.check_vbo", args


def require_context(context: RuntimeReadContext | CandidateReadContext) -> None:
    if type(context) not in (RuntimeReadContext, CandidateReadContext):
        raise DomainError("read_context_invalid", "读取归属上下文无效")


def require_bc(context: RuntimeReadContext | CandidateReadContext, bc_id: str) -> None:
    try:
        require_id(bc_id)
    except ValueError:
        raise DomainError("read_context_invalid", "读取归属上下文无效") from None
    if isinstance(context, RuntimeReadContext) and bc_id != context.bc_id:
        raise DomainError("read_bc_mismatch", "读取 BC 与绑定连接不一致")


def object_data(response: McpBusinessResponse) -> dict[str, Any]:
    if not isinstance(response.data, dict):
        raise legacy_accounts.schema_error()
    return response.data


def directory_page[T](
    data: dict[str, Any],
    *,
    items: tuple[T, ...],
    page: int,
    page_size: int,
    evidence: CallEvidence,
) -> DirectoryPage[T]:
    _, last = legacy_accounts.paged_rows(data, page=page, page_size=page_size)
    info = data["page_info"]
    try:
        return DirectoryPage(
            items=items,
            page=page,
            page_size=page_size,
            total_pages=info["total_page"],
            total_number=info.get("total_number"),
            last=last,
            evidence=evidence,
        )
    except ValueError, TypeError:
        raise legacy_accounts.schema_error() from None


def _unique_rows(data: dict[str, Any], field: str) -> dict[str, dict[str, Any]]:
    rows = legacy_accounts.object_list(data)
    if len(rows) > 100000:
        raise legacy_accounts.schema_error()
    result = {}
    for row in rows:
        identity = legacy_accounts.external_id(row, field)
        if identity in result:
            raise legacy_accounts.schema_error()
        result[identity] = row
    return result


class AccountsReadAdapter(ABC):
    """两个通道共享目录交集与验证；每个 _call 只发送一次固定操作。"""

    def __init__(
        self,
        *,
        context: RuntimeReadContext | CandidateReadContext,
        authorization: AuthorizationFacts,
    ) -> None:
        require_context(context)
        if not isinstance(authorization, AuthorizationFacts):
            raise DomainError("authorization_facts_invalid", "缺少可验证授权来源")
        self._context = context
        self._authorization = authorization

    @abstractmethod
    def _call(
        self, operation: str, arguments: dict[str, Any]
    ) -> McpBusinessResponse: ...

    def authorization_facts(self) -> AuthorizationFacts:
        # 工厂传入已验证 token/授权记录；工具可见性和业务目录不产生授权旗标。
        return self._authorization

    def _page_args(self, page: int, page_size: int) -> dict[str, Any]:
        try:
            require_page(page, page_size)
        except ValueError:
            raise legacy_accounts.schema_error() from None
        return {"page": page, "page_size": page_size}

    def business_centers(
        self, *, page: int, page_size: int
    ) -> DirectoryPage[BusinessCenterFact]:
        if not isinstance(self._context, CandidateReadContext):
            raise DomainError("read_directory_forbidden", "运行连接不可扩张 BC 目录")
        return self._business_centers(page=page, page_size=page_size)

    def _business_centers(
        self, *, page: int, page_size: int
    ) -> DirectoryPage[BusinessCenterFact]:
        response = self._call("accounts.list_bcs", self._page_args(page, page_size))
        data = object_data(response)
        rows, _ = legacy_accounts.paged_rows(data, page=page, page_size=page_size)
        items = tuple(
            BusinessCenterFact(*legacy_accounts.business_center(row)) for row in rows
        )
        return directory_page(
            data,
            items=items,
            page=page,
            page_size=page_size,
            evidence=response.evidence,
        )

    def _assets(
        self, bc_id: str, page: int, page_size: int
    ) -> tuple[McpBusinessResponse, dict[str, Any]]:
        require_bc(self._context, bc_id)
        args = self._page_args(page, page_size)
        args.update(bc_id=bc_id, asset_type="ADVERTISER")
        response = self._call("accounts.list_bc_assets", args)
        data = object_data(response)
        legacy_accounts.paged_rows(data, page=page, page_size=page_size)
        _unique_rows(data, "asset_id")
        for row in data["list"]:
            if row.get("asset_type") != "ADVERTISER":
                raise legacy_accounts.schema_error()
        return response, data

    def roles(
        self, *, bc_id: str, page: int, page_size: int
    ) -> DirectoryPage[AccountRoleFact]:
        response, data = self._assets(bc_id, page, page_size)
        try:
            items = tuple(
                AccountRoleFact(
                    legacy_accounts.external_id(row, "asset_id"),
                    row.get("advertiser_role"),
                )
                for row in data["list"]
            )
        except ValueError, TypeError:
            raise legacy_accounts.schema_error() from None
        return directory_page(
            data,
            items=items,
            page=page,
            page_size=page_size,
            evidence=response.evidence,
        )

    def advertisers(
        self, *, bc_id: str, page: int, page_size: int
    ) -> DirectoryPage[AdvertiserFact]:
        response, data = self._assets(bc_id, page, page_size)
        authorized = _unique_rows(
            object_data(self._call("accounts.list_authorized_advertisers", {})),
            "advertiser_id",
        )
        assets = _unique_rows(data, "asset_id")
        ids = [identity for identity in assets if identity in authorized]
        details = (
            _unique_rows(
                object_data(
                    self._call("accounts.get_advertisers", {"advertiser_ids": ids})
                ),
                "advertiser_id",
            )
            if ids
            else {}
        )
        if set(details) != set(ids):
            raise legacy_accounts.schema_error()
        items = []
        for identity, row in assets.items():
            # 未授权行保留 BC 分页计数，但绝不请求其详情或推导写入许可。
            detail = (
                legacy_accounts.detail_row(details[identity])
                if identity in details
                else {
                    "advertiser_id": identity,
                    "name": legacy_accounts.asset_row(row)["name"],
                    "currency": "",
                    "timezone": "",
                    "remote_status": "UNKNOWN",
                }
            )
            items.append(AdvertiserFact(**detail, authorized=identity in authorized))
        return directory_page(
            data,
            items=tuple(items),
            page=page,
            page_size=page_size,
            evidence=response.evidence,
        )
