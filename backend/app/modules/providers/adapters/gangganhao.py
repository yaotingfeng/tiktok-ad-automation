"""刚刚好分销门户 adapter, ported from the local portal CLI."""

from __future__ import annotations

from typing import Any

import httpx

from app.core.errors import DomainError
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

BASE = "https://gateway.dorareel.com/portal/distributor"
PAGE_SIZE = 100


class GangganhaoClient:
    def __init__(
        self, http: httpx.Client, *, token: str = "", application_id: str = ""
    ):
        self.http, self.token, self.application_id = http, token, application_id

    @classmethod
    def login(
        cls, http: httpx.Client, *, portal_id: int | str, username: str, password: str
    ) -> GangganhaoClient:
        portal_id = positive(portal_id)
        client = cls(http)
        data = client._request(
            "POST",
            "/login",
            json={"id": portal_id, "name": username, "password": password},
        )
        if not isinstance(data, dict) or not isinstance(data.get("token"), str) or not data["token"]:
            raise failure("provider_session_expired")
        client.token = data["token"]
        return client

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
            headers={"authorization": f"Bearer {self.token}"} if self.token else {},
            json=json,
            write=write,
        )
        if body.get("code") == 401:
            raise failure("provider_session_expired")
        if body.get("code") != 0:
            raise failure("provider_rejected")
        if "data" not in body:
            raise failure("provider_schema_unsupported")
        return body["data"]

    def discover_applications(self) -> list[JsonDict]:
        data = self._request("GET", "/apps")
        rows = data.get("list") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise failure("provider_application_discovery_unverified")
        result: list[JsonDict] = []
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                raise failure("provider_schema_unsupported")
            external = external_id(row.get("authorizerAppId"))
            name = row.get("appName")
            if external in seen or not isinstance(name, str) or not name.strip():
                raise failure("provider_schema_unsupported")
            seen.add(external)
            config = {
                "delivery_mode": row["deliveryMode"]
                if isinstance(row.get("deliveryMode"), str)
                else "iaa"
            }
            result.append(
                {
                    "external_id": external,
                    "name": name,
                    "channel_config": config,
                    "tiktok_minis_id": None,
                }
            )
        if not result:
            raise failure("provider_application_discovery_unverified")
        return result

    def search(self, title: str, cursor: str | None) -> SearchPage:
        page = positive(cursor or 1)
        query = {"page": page, "size": PAGE_SIZE, "keyword": title}
        if self.application_id:
            query["authorizerAppIds[]"] = self.application_id
        data = self._request(
            "GET",
            "/series",
            query=query,
        )
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise failure("provider_schema_unsupported")
        rows = data["list"]
        total = data.get("total")
        if type(total) is not int or total < 0:
            raise failure("provider_schema_unsupported")
        items: list[DramaCandidate] = []
        for row in rows:
            if not isinstance(row, dict):
                raise failure("provider_schema_unsupported")
            row_title = row.get("seriesTitle")
            if not isinstance(row_title, str):
                raise failure("provider_schema_unsupported")
            if self.application_id and str(row.get("authorizerAppId")) != str(
                self.application_id
            ):
                continue
            if row_title == title:
                items.append(
                    DramaCandidate(
                        external_drama_id=external_id(row.get("publishId")),
                        display_drama_id=external_id(row.get("publishId")),
                        title=row_title,
                        language=row.get("language") if isinstance(row.get("language"), str) else None,
                    )
                )
        complete = page * PAGE_SIZE >= total
        return SearchPage(items=items, next_cursor=None if complete else str(page + 1), complete=complete)

    def lookup_link(self, drama_id: str, config: JsonDict, cursor: str | None) -> LinkLookupPage:
        normalized = self._validate_config(config, require_template=False)
        if "authorizer_app_id" not in normalized:
            raise failure("provider_request_invalid")
        page = positive(cursor or 1)
        data = self._request(
            "GET",
            "/campaign-links",
            query={
                "page": page,
                "size": PAGE_SIZE,
                "seriesId": normalized["series_id"],
                "authorizerAppId": normalized["authorizer_app_id"],
            },
        )
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise failure("provider_schema_unsupported")
        rows = data["list"]
        total = data.get("total")
        if type(total) is not int or total < 0:
            raise failure("provider_schema_unsupported")
        matched: list[JsonDict] = []
        for row in rows:
            if not isinstance(row, dict):
                raise failure("provider_schema_unsupported")
            if str(row.get("platformPublishId", row.get("publishId", drama_id))) != str(drama_id):
                continue
            link_id = row.get("id")
            if link_id is None:
                continue
            detail = self._request("GET", f"/campaign-links/{external_id(link_id)}")
            if not isinstance(detail, dict):
                raise failure("provider_schema_unsupported")
            if self._matches(detail, normalized):
                matched.append(detail)
        complete = page * PAGE_SIZE >= total
        return LinkLookupPage(items=matched, next_cursor=None if complete else str(page + 1), complete=complete)

    def series_detail(self, publish_id: str) -> JsonDict:
        data = self._request("GET", f"/series/{external_id(publish_id)}")
        if not isinstance(data, dict):
            raise failure("provider_schema_unsupported")
        detail = data.get("detail") if isinstance(data.get("detail"), dict) else data
        publish = detail.get("Publish", {}) if isinstance(detail, dict) else {}
        series = detail.get("Series", {}) if isinstance(detail, dict) else {}
        if not isinstance(publish, dict) or not isinstance(series, dict):
            raise failure("provider_schema_unsupported")
        return {
            "publish_id": external_id(data.get("publishId", publish.get("id", publish_id))),
            "series_id": external_id(publish.get("seriesId", series.get("id"))),
            "series_title": string(publish.get("seriesTitle", series.get("title", ""))),
            # 详情接口有版本会省略变现方式；工作流随后回退到已选应用的目录事实。
            "delivery_mode": publish.get("deliveryMode")
            if isinstance(publish.get("deliveryMode"), str)
            else None,
        }

    def create_link(self, drama_id: str, config: JsonDict) -> LinkReceipt:
        normalized = self._validate_config(config, require_template=True)
        body = {
            "authorizerAppId": normalized["authorizer_app_id"],
            "platformPublishId": positive(drama_id),
            "seriesId": normalized["series_id"],
            "seriesTitle": normalized["series_title"],
            "episodeSeq": normalized["episode_seq"],
            "freeEpisodeCount": normalized["free_episode_count"],
        }
        for key in ("payment_template_id", "name"):
            if key in normalized:
                body[{"payment_template_id": "paymentTemplateId", "name": "name"}[key]] = normalized[key]
        data = self._request("POST", "/campaign-link", json=body, write=True)
        return self._receipt(data, normalized)

    def read_link(self, remote_id: str) -> LinkReceipt:
        data = self._request("GET", f"/campaign-links/{external_id(remote_id)}")
        if not isinstance(data, dict):
            raise failure("provider_result_unknown")
        try:
            free_episode_count = int(data.get("freeEpisodeCount"))
        except (TypeError, ValueError):
            raise failure("provider_result_unknown") from None
        if free_episode_count < 0:
            raise failure("provider_result_unknown")
        series_title = data.get("seriesTitle")
        delivery_mode = data.get("deliveryMode")
        config: JsonDict = {
            "authorizer_app_id": positive(data.get("authorizerAppId")),
            "series_id": positive(data.get("seriesId")),
            "series_title": series_title if isinstance(series_title, str) else "",
            "free_episode_count": free_episode_count,
            "episode_seq": positive(data.get("episodeSeq")),
            "delivery_mode": delivery_mode if isinstance(delivery_mode, str) else "iaa",
        }
        template_id = data.get("paymentTemplateId")
        if template_id not in (None, "", 0):
            config["payment_template_id"] = positive(template_id)
        if isinstance(data.get("paymentTemplateName"), str):
            config["payment_template_name"] = data["paymentTemplateName"]
        if isinstance(data.get("name"), str) and data["name"].strip():
            config["name"] = data["name"].strip()
        normalized = self._validate_config(config, require_template=False)
        return self._receipt(data, normalized)

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

    def capabilities(self, application: Any = None):
        return capabilities_for_kind("gangganhao", application)

    @staticmethod
    def _matches(detail: JsonDict, config: JsonDict) -> bool:
        if str(detail.get("authorizerAppId")) != str(config["authorizer_app_id"]):
            return False
        if str(detail.get("seriesId")) != str(config["series_id"]):
            return False
        if positive(detail.get("freeEpisodeCount")) != config["free_episode_count"]:
            return False
        if positive(detail.get("episodeSeq")) != config["episode_seq"]:
            return False
        wanted = config.get("payment_template_id")
        if wanted is None:
            return detail.get("paymentTemplateId") in (None, "", 0)
        return str(detail.get("paymentTemplateId")) == str(wanted)

    @staticmethod
    def _validate_config(config: JsonDict, *, require_template: bool) -> JsonDict:
        allowed = {
            "authorizer_app_id",
            "series_id",
            "series_title",
            "free_episode_count",
            "episode_seq",
            "payment_template_id",
            "payment_template_name",
            "name",
            "delivery_mode",
        }
        if not isinstance(config, dict) or set(config) - allowed:
            raise failure("provider_request_invalid")
        try:
            mode = str(config.get("delivery_mode", "iaa")).lower()
            normalized = {
                "series_id": positive(config["series_id"]),
                "series_title": string(config.get("series_title", "")).strip(),
                "free_episode_count": int(config["free_episode_count"]),
                "episode_seq": positive(config["episode_seq"]),
            }
        except (KeyError, TypeError, ValueError):
            raise failure("provider_request_invalid") from None
        if require_template and not normalized["series_title"]:
            raise failure("provider_request_invalid")
        if "authorizer_app_id" in config:
            normalized["authorizer_app_id"] = positive(config["authorizer_app_id"])
        elif require_template:
            raise failure("provider_request_invalid")
        if normalized["free_episode_count"] < 0:
            raise failure("provider_request_invalid")
        if normalized["free_episode_count"] == 0 and normalized["episode_seq"] != 1:
            raise failure("provider_request_invalid")
        if normalized["episode_seq"] > max(normalized["free_episode_count"], 1):
            raise failure("provider_request_invalid")
        template_required = mode in {"iap", "mixed"}
        if template_required and "payment_template_id" not in config:
            raise failure("provider_request_invalid")
        if require_template and template_required:
            template_id = positive(config.get("payment_template_id"))
            normalized["payment_template_id"] = template_id
        elif "payment_template_id" in config:
            normalized["payment_template_id"] = positive(config["payment_template_id"])
        if config.get("payment_template_name") is not None:
            normalized["payment_template_name"] = string(config["payment_template_name"])
        if config.get("name") is not None:
            name = string(config["name"]).strip()
            if name:
                normalized["name"] = name
        return normalized

    @staticmethod
    def _receipt(data: Any, config: JsonDict) -> LinkReceipt:
        if not isinstance(data, dict):
            raise failure("provider_result_unknown")
        remote_id = data.get("id", data.get("linkId"))
        url = data.get("minisLink")
        if remote_id is None or not isinstance(url, str) or not url:
            raise failure("provider_result_unknown")
        name = data.get("name") or ""
        link_code = data.get("linkCode") or ""
        try:
            return LinkReceipt(
                remote_id=external_id(remote_id),
                url=url,
                protected_base=name or link_code,
                attribution={"linkCode": link_code, "name": name},
                config=config,
            )
        except (ValueError, DomainError):
            raise failure("provider_result_unknown") from None
