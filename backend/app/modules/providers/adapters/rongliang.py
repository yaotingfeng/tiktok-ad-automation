"""容量分销后台 adapter with strict post-create batch read-back."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx

from app.modules.providers.capabilities import capabilities_for_kind
from app.modules.providers.schemas import DramaCandidate

from .contract import (
    JsonDict,
    LinkLookupPage,
    LinkReceipt,
    SearchPage,
    external_id,
    failure,
    positive,
    request_json,
    string,
)

BASE = "https://distribution.wolftv.online/manage/ocean/management/distribution"
PAGE_SIZE = 100
COOKIE_DOMAIN = "distribution.wolftv.online"


class RongliangClient:
    def __init__(self, http: httpx.Client, *, token: str = ""):
        self.http, self.token = http, token
        # 容量网页依赖登录响应下发的完整 Cookie 集合。旧实现手工只发
        # dist_token，会丢掉同一登录会话的其他 Cookie，导致后台返回
        # HTTP 500 / code=403 (admin not login)。恢复已缓存 token 时也要
        # 把 token 注入同一个 httpx Cookie jar，保持首次登录和自动重登一致。
        if token and not any(cookie.name == "dist_token" for cookie in self.http.cookies.jar):
            self.http.cookies.set("dist_token", token, domain=COOKIE_DOMAIN, path="/")

    @classmethod
    def login(cls, http: httpx.Client, *, email: str, password: str) -> RongliangClient:
        encoded = urlencode({"email": email, "password": password})
        body, _ = request_json(
            http,
            "POST",
            BASE + "/auth/login",
            headers={"content-type": "application/x-www-form-urlencoded; charset=UTF-8"},
            content=encoded,
        )
        # 登录成功响应当前没有 code 字段，只有 token 和用户信息；与 CLI
        # 及浏览器实际响应保持一致，只有明确的非零 code 才视为认证失败。
        if body.get("code") not in (None, 0, "0") or not isinstance(body.get("token"), str):
            raise failure("provider_auth_failed")
        return cls(http, token=body["token"])

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: JsonDict | None = None,
        json: JsonDict | None = None,
        write: bool = False,
    ) -> Any:
        body, _ = request_json(
            self.http,
            method,
            BASE + path,
            params=query,
            json=json,
            write=write,
        )
        code = body.get("code")
        if code in {"user-2", 401, "401", 403, "403"}:
            raise failure("provider_session_expired")
        if code not in (None, 0, "0"):
            raise failure("provider_rejected")
        return body.get("data", body)

    def discover_applications(self) -> list[JsonDict]:
        data = self._request("GET", "/link/form_data")
        options = data.get("packageOptions") if isinstance(data, dict) else None
        if not isinstance(options, list):
            raise failure("provider_application_discovery_unverified")
        result: list[JsonDict] = []
        seen: set[str] = set()
        for row in options:
            if not isinstance(row, dict) or row.get("clientType") != "TIKTOK_MINI_PROGRAM":
                continue
            client_id = external_id(row.get("clientId"))
            name = row.get("clientName")
            if client_id in seen or not isinstance(name, str) or not name.strip():
                raise failure("provider_schema_unsupported")
            seen.add(client_id)
            result.append(
                {
                    "external_id": client_id,
                    "name": name,
                    "channel_config": {"platform": 1},
                    "tiktok_minis_id": None,
                }
            )
        if not result:
            raise failure("provider_application_discovery_unverified")
        return result

    def search(self, title: str, cursor: str | None) -> SearchPage:
        page = positive(cursor or 1)
        data = self._request(
            "GET",
            "/compilations/page",
            query={"pageNumber": page, "pageSize": PAGE_SIZE, "compilationsName": title},
        )
        if not isinstance(data, dict) or not isinstance(data.get("records"), list):
            raise failure("provider_schema_unsupported")
        total = data.get("total")
        if type(total) is not int or total < 0:
            raise failure("provider_schema_unsupported")
        items: list[DramaCandidate] = []
        for row in data["records"]:
            if not isinstance(row, dict):
                raise failure("provider_schema_unsupported")
            row_title = row.get("originalTitle") or row.get("compilationsName")
            if not isinstance(row_title, str):
                raise failure("provider_schema_unsupported")
            if row_title == title:
                items.append(
                    DramaCandidate(
                        external_drama_id=external_id(row.get("compilationsId")),
                        display_drama_id=external_id(row.get("compilationsId")),
                        title=row_title,
                        language=row.get("languageName") if isinstance(row.get("languageName"), str) else None,
                    )
                )
        complete = page * PAGE_SIZE >= total
        return SearchPage(items=items, next_cursor=None if complete else str(page + 1), complete=complete)

    def episode_options(self, compilation_id: str) -> list[JsonDict]:
        data = self._request(
            "GET",
            "/link/episodic_dramas",
            query={"compilationsId": external_id(compilation_id)},
        )
        if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
            raise failure("provider_schema_unsupported")
        return data

    def lookup_link(self, drama_id: str, config: JsonDict, cursor: str | None) -> LinkLookupPage:
        normalized = self._validate_config(config, require_episode=True)
        page = positive(cursor or 1)
        data = self._request(
            "GET",
            "/link/page",
            query={
                "pageNumber": page,
                "pageSize": PAGE_SIZE,
                "clientId": normalized["client_id"],
                "deliverPlatform": normalized["platform"],
            },
        )
        if not isinstance(data, dict) or not isinstance(data.get("records"), list):
            raise failure("provider_schema_unsupported")
        total = data.get("total")
        if type(total) is not int or total < 0:
            raise failure("provider_schema_unsupported")
        matched: list[JsonDict] = []
        for row in data["records"]:
            if not isinstance(row, dict):
                raise failure("provider_schema_unsupported")
            if self._matches(row, drama_id, normalized):
                detail = dict(row)
                batch = detail.get("batchId")
                if not isinstance(batch, str) or not batch:
                    raise failure("provider_schema_unsupported")
                detail.update(self._link_url(batch))
                matched.append(detail)
        complete = page * PAGE_SIZE >= total
        return LinkLookupPage(items=matched, next_cursor=None if complete else str(page + 1), complete=complete)

    def create_link(self, drama_id: str, config: JsonDict) -> LinkReceipt:
        normalized = self._validate_config(config, require_episode=True)
        body = {
            "clientId": normalized["client_id"],
            "deliverPlatform": normalized["platform"],
            "deliveryType": normalized["delivery_type"],
            "linkType": normalized["link_type"],
            "compilationsId": positive(drama_id),
            "compilationsAlias": normalized.get("alias", ""),
            "episodicDramaId": normalized["episodic_drama_id"],
        }
        result = self._request("POST", "/link/create", json=body, write=True)
        if result is not True and not (isinstance(result, dict) and result.get("success") is True):
            raise failure("provider_result_unknown")
        data = self._request(
            "GET",
            "/link/page",
            query={
                "pageNumber": 1,
                "pageSize": PAGE_SIZE,
                "clientId": normalized["client_id"],
                "deliverPlatform": normalized["platform"],
            },
        )
        if not isinstance(data, dict) or not isinstance(data.get("records"), list):
            raise failure("provider_result_unknown")
        rows = [row for row in data["records"] if isinstance(row, dict) and self._matches(row, drama_id, normalized)]
        if len(rows) != 1:
            raise failure("provider_result_unknown")
        batch = rows[0].get("batchId")
        if not isinstance(batch, str) or not batch:
            raise failure("provider_result_unknown")
        url_data = self._link_url(batch)
        return self._receipt(batch, url_data, normalized)

    def read_link(self, remote_id: str) -> LinkReceipt:
        batch_id = external_id(remote_id)
        data = self._request(
            "GET",
            "/link/page",
            query={
                "pageNumber": 1,
                "pageSize": PAGE_SIZE,
                "batchId": batch_id,
            },
        )
        if not isinstance(data, dict) or not isinstance(data.get("records"), list):
            raise failure("provider_result_unknown")
        rows = [
            row
            for row in data["records"]
            if isinstance(row, dict) and str(row.get("batchId")) == batch_id
        ]
        if len(rows) != 1:
            raise failure("provider_result_unknown")
        row = rows[0]
        config: JsonDict = {
            "client_id": positive(row.get("clientId")),
            "episodic_drama_id": positive(row.get("episodicDramaId")),
            "platform": positive(row.get("deliverPlatform", 1)),
            "delivery_type": positive(row.get("deliveryType", 1)),
            "link_type": positive(row.get("linkType", 2)),
        }
        if isinstance(row.get("compilationsAlias"), str):
            config["alias"] = row["compilationsAlias"]
        url_data = self._link_url(batch_id)
        return self._receipt(batch_id, url_data, config)

    def capabilities(self, application: Any = None):
        return capabilities_for_kind("rongliang", application)

    def create_step(self, step: str, payload: JsonDict) -> JsonDict:
        if step != "create" or not isinstance(payload.get("config"), dict):
            raise failure("provider_request_invalid")
        receipt = self.create_link(str(payload.get("vid")), payload["config"])
        return {
            "remote_id": receipt.remote_id,
            "url": receipt.url,
            "protected_base": receipt.protected_base,
            "attribution": receipt.attribution,
            "config": receipt.config,
        }

    def _link_url(self, batch_id: str) -> JsonDict:
        data = self._request("GET", "/link/url", query={"batchId": batch_id})
        if not isinstance(data, dict) or not isinstance(data.get("deepLink"), str) or not data["deepLink"]:
            raise failure("provider_result_unknown")
        return data

    @staticmethod
    def _matches(row: JsonDict, drama_id: str, config: JsonDict) -> bool:
        return (
            str(row.get("compilationsId")) == str(drama_id)
            and str(row.get("episodicDramaId")) == str(config["episodic_drama_id"])
            and str(row.get("clientId")) == str(config["client_id"])
            and str(row.get("deliverPlatform")) == str(config["platform"])
        )

    @staticmethod
    def _validate_config(config: JsonDict, *, require_episode: bool) -> JsonDict:
        allowed = {
            "client_id",
            "episodic_drama_id",
            "platform",
            "delivery_type",
            "link_type",
            "alias",
        }
        if not isinstance(config, dict) or set(config) - allowed:
            raise failure("provider_request_invalid")
        try:
            normalized = {
                "client_id": positive(config["client_id"]),
                "platform": positive(config.get("platform", 1)),
                "delivery_type": positive(config.get("delivery_type", 1)),
                "link_type": positive(config.get("link_type", 2)),
            }
            if require_episode:
                normalized["episodic_drama_id"] = positive(config["episodic_drama_id"])
        except (KeyError, TypeError, ValueError):
            raise failure("provider_request_invalid") from None
        if config.get("alias") is not None:
            normalized["alias"] = string(config["alias"]).strip()
        return normalized

    @staticmethod
    def _receipt(remote_id: str, data: JsonDict, config: JsonDict) -> LinkReceipt:
        try:
            return LinkReceipt(
                remote_id=remote_id,
                url=string(data["deepLink"]),
                protected_base=string(data.get("planName") or data.get("adGroupName") or ""),
                attribution={
                    "planName": data.get("planName") or "",
                    "adGroupName": data.get("adGroupName") or "",
                },
                config=config,
            )
        except (KeyError, ValueError):
            raise failure("provider_result_unknown") from None
