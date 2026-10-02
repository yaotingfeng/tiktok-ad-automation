"""兑吧奇境分销 HTTP adapter, ported from the local protocol CLI."""

from __future__ import annotations

from typing import Any

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

BASE = "https://qjad.dui88.com/api/dist"
PAGE_SIZE = 100


class DuibaClient:
    """A tenant-owned client; bearer state is never stored on the HTTP client."""

    safe_idempotent_create_without_lookup = True

    def __init__(self, http: httpx.Client, *, token: str = ""):
        self.http, self.token = http, token

    @classmethod
    def login(cls, http: httpx.Client, *, account: str, password: str) -> DuibaClient:
        client = cls(http)
        data = client._request("POST", "/auth/login", json={"account": account, "password": password})
        if not isinstance(data, dict) or not isinstance(data.get("token"), str) or not data["token"]:
            raise failure("provider_session_expired")
        client.token = data["token"]
        return client

    def _request(self, method: str, path: str, *, query: JsonDict | None = None, json: JsonDict | None = None, write: bool = False) -> Any:
        headers = {"authorization": f"Bearer {self.token}"} if self.token else {}
        body, _ = request_json(
            self.http,
            method,
            BASE + path,
            params=query,
            headers=headers,
            json=json,
            write=write,
        )
        if body.get("success") is not True:
            code = str(body.get("code", ""))
            if code == "401":
                raise failure("provider_session_expired")
            raise failure("provider_rejected")
        if "data" not in body:
            raise failure("provider_schema_unsupported")
        return body["data"]

    def discover_applications(self) -> list[JsonDict]:
        data = self._request("GET", "/miniapp/myList")
        if not isinstance(data, list):
            raise failure("provider_application_discovery_unverified")
        result: list[JsonDict] = []
        seen: set[str] = set()
        for row in data:
            if not isinstance(row, dict):
                raise failure("provider_schema_unsupported")
            app_id = external_id(row.get("id"))
            if app_id in seen:
                raise failure("provider_schema_unsupported")
            seen.add(app_id)
            name = row.get("name")
            if not isinstance(name, str) or not name.strip():
                raise failure("provider_schema_unsupported")
            channel_config = {
                key: row[key]
                for key in ("appId", "clientKey", "language", "countries")
                if key in row and isinstance(row[key], (str, list))
            }
            result.append(
                {
                    "external_id": app_id,
                    "name": name,
                    "channel_config": channel_config,
                    "tiktok_minis_id": None,
                }
            )
        return result

    def search(self, title: str, cursor: str | None) -> SearchPage:
        page = positive(cursor or 1)
        data = self._request(
            "GET",
            "/drama/page",
            query={"pageNo": page, "pageSize": PAGE_SIZE, "keyword": title},
        )
        rows, next_cursor, complete = self._paged(data, page)
        items: list[DramaCandidate] = []
        for row in rows:
            if not isinstance(row, dict):
                raise failure("provider_schema_unsupported")
            row_title = row.get("title")
            if not isinstance(row_title, str):
                raise failure("provider_schema_unsupported")
            if row_title != title:
                continue
            items.append(
                DramaCandidate(
                    external_drama_id=external_id(row.get("id")),
                    display_drama_id=external_id(row.get("id")),
                    title=row_title,
                    language=row.get("language") if isinstance(row.get("language"), str) else None,
                )
            )
        return SearchPage(items=items, next_cursor=next_cursor, complete=complete)

    @staticmethod
    def _paged(data: Any, page: int) -> tuple[list[JsonDict], str | None, bool]:
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise failure("provider_schema_unsupported")
        rows = data["list"]
        if any(not isinstance(row, dict) for row in rows):
            raise failure("provider_schema_unsupported")
        total = data.get("total")
        if total is not None and (type(total) is not int or total < 0):
            raise failure("provider_schema_unsupported")
        if total is None:
            complete = len(rows) < PAGE_SIZE
        else:
            if total < (page - 1) * PAGE_SIZE + len(rows):
                raise failure("provider_schema_unsupported")
            complete = page * PAGE_SIZE >= total
        return rows, None if complete else str(page + 1), complete

    def preview_drama(self, drama_id: str) -> list[JsonDict]:
        data = self._request("GET", "/drama/preview", query={"id": external_id(drama_id)})
        if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
            raise failure("provider_schema_unsupported")
        result = []
        for row in data:
            serial = positive(row.get("serialNo"))
            episode_id = row.get("lc671EpisodeId")
            if not isinstance(episode_id, str) or not episode_id.strip():
                raise failure("provider_schema_unsupported")
            result.append({"serialNo": serial, "lc671EpisodeId": episode_id})
        return result

    def copy_miniapps(self, drama_id: str) -> list[str]:
        data = self._request("GET", "/drama/copy-miniapps", query={"id": external_id(drama_id)})
        if not isinstance(data, list):
            raise failure("provider_schema_unsupported")
        return [external_id(item) for item in data]

    def lookup_link(self, drama_id: str, config: JsonDict, cursor: str | None) -> LinkLookupPage:
        self._validate_config(config, require_lc=False)
        page = positive(cursor or 1)
        data = self._request(
            "GET",
            "/link/page",
            query={"pageNo": page, "pageSize": PAGE_SIZE},
        )
        rows, next_cursor, complete = self._paged(data, page)
        matched = [
            row
            for row in rows
            if str(row.get("dramaId")) == str(drama_id)
            and str(row.get("miniappId", row.get("miniappAppId"))) == str(config["miniapp_id"])
            and positive(row.get("defaultEpisode")) == config["episode"]
            and positive(row.get("cardPointEpisode")) == config["card_point_episode"]
        ]
        return LinkLookupPage(items=matched, next_cursor=next_cursor, complete=complete)

    def create_link(self, drama_id: str, config: JsonDict) -> LinkReceipt:
        normalized = self._validate_config(config, require_lc=True)
        data = self._request(
            "POST",
            "/link/create",
            json={
                "dramaId": external_id(drama_id),
                "miniappId": normalized["miniapp_id"],
                "defaultEpisode": normalized["episode"],
                "cardPointEpisode": normalized["card_point_episode"],
                "defaultEpisodeLcId": normalized["default_episode_lc_id"],
                "cardPointEpisodeLcId": normalized["card_point_episode_lc_id"],
            },
            write=True,
        )
        if not isinstance(data, dict):
            raise failure("provider_result_unknown")
        remote_id = data.get("linkNo", data.get("id"))
        url = data.get("minisLink")
        if not isinstance(remote_id, (str, int)) or not str(remote_id) or not isinstance(url, str):
            raise failure("provider_result_unknown")
        return self._receipt(
            remote_id=str(remote_id),
            url=url,
            config=normalized,
            attribution={"linkNo": str(remote_id), "miniapp_id": str(normalized["miniapp_id"])},
        )

    def read_link(self, remote_id: str) -> LinkReceipt:
        data = self._request("GET", "/link/page", query={"linkNo": remote_id, "pageNo": 1, "pageSize": 20})
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise failure("provider_result_unknown")
        rows = [row for row in data["list"] if isinstance(row, dict) and str(row.get("linkNo", row.get("id"))) == str(remote_id)]
        if len(rows) != 1:
            raise failure("provider_result_unknown")
        row = rows[0]
        config = {
            "miniapp_id": str(row.get("miniappId", row.get("miniappAppId"))),
            "episode": positive(row.get("defaultEpisode")),
            "card_point_episode": positive(row.get("cardPointEpisode")),
        }
        url = row.get("minisLink")
        if not isinstance(url, str):
            raise failure("provider_result_unknown")
        return self._receipt(
            remote_id=str(remote_id),
            url=url,
            config=config,
            attribution={"linkNo": str(remote_id), "miniapp_id": config["miniapp_id"]},
        )

    def create_step(self, step: str, payload: JsonDict) -> JsonDict:
        """Compatibility write hook for the durable provider workflow."""

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

    def capabilities(self, application: Any = None):
        return capabilities_for_kind("duiba", application)

    @staticmethod
    def _validate_config(config: JsonDict, *, require_lc: bool) -> JsonDict:
        allowed = {
            "miniapp_id",
            "episode",
            "card_point_episode",
            "default_episode_lc_id",
            "card_point_episode_lc_id",
        }
        if not isinstance(config, dict) or set(config) - allowed:
            raise failure("provider_request_invalid")
        try:
            normalized = {
                "miniapp_id": positive(config["miniapp_id"]),
                "episode": positive(config["episode"]),
                "card_point_episode": positive(config["card_point_episode"]),
            }
        except (KeyError, TypeError):
            raise failure("provider_request_invalid") from None
        if require_lc:
            for key in ("default_episode_lc_id", "card_point_episode_lc_id"):
                value = config.get(key)
                if not isinstance(value, str) or not value.strip():
                    raise failure("provider_request_invalid")
                normalized[key] = value
        return normalized

    @staticmethod
    def _receipt(*, remote_id: str, url: str, config: JsonDict, attribution: JsonDict) -> LinkReceipt:
        try:
            return LinkReceipt(
                remote_id=remote_id,
                url=string(url),
                protected_base="",
                attribution=attribution,
                config=config,
            )
        except ValueError:
            raise failure("provider_schema_unsupported") from None
