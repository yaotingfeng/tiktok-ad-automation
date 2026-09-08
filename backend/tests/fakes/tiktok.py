"""Synthetic Smart+ official-call double reusable by offline execution tests."""

import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

import business_api_client as sdk


@dataclass
class _Completed:
    response: Any

    def get(self) -> Any:
        return self.response


class FakeTikTokAPI:
    """Closed create/get surface. Any status mutation is a test failure.

    Patch an owned official client's call_api with this method. Direct calls
    return InlineResponse200; async_req=True mirrors the official future API.
    Actual serializer/wire tests separately patch urllib3 transport.
    """

    def __init__(self) -> None:
        self.store: dict[str, dict[str, dict[str, Any]]] = {
            "campaign": {},
            "adgroup": {},
            "ad": {},
        }
        self.calls: list[dict[str, Any]] = []

    def call_api(
        self, resource_path: str, http_method: str, *args: Any, **kwargs: Any
    ) -> Any:
        parts = resource_path.split("/")
        assert len(parts) == 7 and parts[:4] == ["", "open_api", "v1.3", "smart_plus"]
        kind, action = parts[4:6]
        assert kind in self.store and action in {"create", "get"}, (
            "Unexpected SDK operation"
        )
        key = {
            "campaign": "campaign_id",
            "adgroup": "adgroup_id",
            "ad": "smart_plus_ad_id",
        }[kind]
        body = deepcopy(kwargs.get("body") or {})
        query = dict(args[1] if len(args) > 1 else kwargs.get("query_params", []))
        self.calls.append(
            {
                "path": resource_path,
                "method": http_method,
                "body": body,
                "query": deepcopy(query),
            }
        )
        if action == "create":
            assert http_method == "POST" and body.get("operation_status") == "ENABLE"
            remote_id = str(900000 + len(self.store[kind]))
            row = {
                **body,
                key: remote_id,
                "operation_status": "ENABLE",
                "secondary_status": "AD_STATUS_AUDIT",
            }
            self.store[kind][remote_id] = row
            response = sdk.InlineResponse200(
                code=0, request_id="fake-create", data=deepcopy(row)
            )
        else:
            assert http_method == "GET"
            rows = [
                row
                for row in self.store[kind].values()
                if row["advertiser_id"] == query["advertiser_id"]
            ]
            filters = query.get("filtering") or {}
            if isinstance(filters, str):
                filters = json.loads(filters)
            assert isinstance(filters, dict)
            # Pinned FilteringSmartPlus*Get schemas: ad-name is not a filter.
            id_filters = {
                "campaign": {"campaign_ids": "campaign_id"},
                "adgroup": {"campaign_ids": "campaign_id", "adgroup_ids": "adgroup_id"},
                "ad": {
                    "campaign_ids": "campaign_id",
                    "adgroup_ids": "adgroup_id",
                    "smart_plus_ad_ids": "smart_plus_ad_id",
                },
            }[kind]
            name = {"campaign": "campaign_name", "adgroup": "adgroup_name"}.get(kind)
            assert set(filters) <= set(id_filters) | ({name} if name else set())
            for plural, singular in id_filters.items():
                if plural in filters:
                    assert isinstance(filters[plural], list)
                    rows = [row for row in rows if row.get(singular) in filters[plural]]
            if name and name in filters:
                rows = [row for row in rows if row.get(name) == filters[name]]
            page, size = int(query.get("page", 1)), int(query.get("page_size", 10))
            assert page >= 1 and 1 <= size <= 1000
            response = sdk.InlineResponse200(
                code=0,
                request_id="fake-read",
                data={
                    "list": deepcopy(rows[(page - 1) * size : page * size]),
                    "page_info": {
                        "page": page,
                        "page_size": size,
                        "total_number": len(rows),
                        "total_page": (len(rows) + size - 1) // size,
                    },
                },
            )
        return _Completed(response) if kwargs.get("async_req") else response
