"""Pinned official read adapters; admission is required for each invocation."""

from typing import Any

import business_api_client

from app.core.errors import DomainError

from .sdk import checked_data


def read_bc_assets(client: Any, *, bc_id: str, page: int, page_size: int) -> dict:
    return checked_data(
        business_api_client.BCApi(client).bc_asset_get(
            bc_id,
            "ADVERTISER",
            client.default_headers["Access-Token"],
            page=page,
            page_size=page_size,
            _request_timeout=(5, 30),
        )
    )


def read_authorized_advertisers(client: Any, *, app_id: str, secret: str) -> dict:
    return checked_data(
        business_api_client.AuthenticationApi(client).oauth2_advertiser_get(
            app_id,
            secret,
            client.default_headers["Access-Token"],
            _request_timeout=(5, 30),
        )
    )


def read_business_centers(client: Any, *, page: int, page_size: int) -> dict:
    return checked_data(
        business_api_client.BCApi(client).bc_get(
            client.default_headers["Access-Token"],
            page=page,
            page_size=page_size,
            _request_timeout=(5, 30),
        )
    )


def read_advertiser_details(client: Any, *, advertiser_ids: list[str]) -> dict:
    return checked_data(
        business_api_client.AccountManagementApi(client).advertiser_info(
            advertiser_ids,
            client.default_headers["Access-Token"],
            _request_timeout=(5, 30),
        )
    )


def schema_error() -> DomainError:
    return DomainError("unsupported_account_schema", "TikTok 账户结构缺少完整分页证据")


def object_list(data: dict) -> list[dict]:
    rows = data.get("list")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise schema_error()
    return rows


def paged_rows(data: dict, *, page: int, page_size: int) -> tuple[list[dict], bool]:
    rows = object_list(data)
    info = data.get("page_info")
    if not isinstance(info, dict):
        raise schema_error()
    current, size, total = (
        info.get(key) for key in ("page", "page_size", "total_page")
    )
    if any(type(value) is not int for value in (current, size, total)):
        raise schema_error()
    if current != page or size != page_size or total < 0 or len(rows) > page_size:
        raise schema_error()
    if total == 0:
        if page != 1 or rows:
            raise schema_error()
        return rows, True
    if page > total or (page < total and not rows):
        raise schema_error()
    return rows, page == total


def external_id(row: dict, key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value or len(value) > 128:
        raise schema_error()
    return value


def business_center(row: dict) -> tuple[str, str]:
    # bc/get returns each BC under bc_info; never reinterpret unrelated IDs.
    info = row.get("bc_info")
    if not isinstance(info, dict):
        raise schema_error()
    return external_id(info, "bc_id"), str(info.get("name") or "")


def asset_row(row: dict) -> dict:
    # The BC asset API identifies advertiser assets with asset_id/asset_name.
    return {
        "advertiser_id": external_id(row, "asset_id"),
        "name": str(row.get("asset_name") or ""),
    }


def detail_row(row: dict) -> dict:
    return {
        "advertiser_id": external_id(row, "advertiser_id"),
        "name": str(row.get("name") or ""),
        "currency": str(row.get("currency") or ""),
        "timezone": str(row.get("timezone") or ""),
        "remote_status": str(row.get("status") or "UNKNOWN"),
    }
