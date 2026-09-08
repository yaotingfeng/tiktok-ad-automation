"""Jiashu protocol translated from local CLI; external acceptance is still pending."""

from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from .contract import (
    JsonDict,
    counted_page,
    external_id,
    failure,
    positive,
    request_json,
    string,
)

BASE = "https://video-wechat-open.eastdrama.net"
PREFIX = "/Oversea/AppChannelConfig/"


class JiashuClient:
    def __init__(
        self,
        http: httpx.Client,
        *,
        session: str,
        application_id: str,
        channel_prefix: str = "",
    ):
        self.http = http
        self.session = session
        self.application_id = application_id
        self.channel_prefix = channel_prefix

    @classmethod
    def login(cls, http: httpx.Client, *, username: str, password: str) -> JiashuClient:
        client = cls(http, session="", application_id="")
        data = client.post(
            "/User/login", {"username": username, "password": password}, login=True
        )
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("session"), str)
            or not data["session"]
        ):
            raise failure("provider_session_expired")
        client.session = data["session"]
        return client

    def post(
        self,
        path: str,
        payload: JsonDict,
        *,
        write: bool = False,
        login: bool = False,
        discover: bool = False,
    ) -> Any:
        params = (
            {"site_type": "video"}
            if login
            else {
                "channel": "" if discover else self.application_id,
                "channel_from": "" if discover else 7,
                "channel_type": "" if discover else 1,
                "site_type": "oversea_video_iaa",
            }
        )
        headers = {
            "session": "" if login else self.session,
            "cookie": "",
            "Origin": "https://m.eastdrama.net",
            "Referer": "https://m.eastdrama.net/",
        }
        body, _ = request_json(
            self.http,
            "POST",
            BASE + path,
            params=params,
            headers=headers,
            json=payload,
            write=write,
        )
        raw_code = body.get("code")
        if type(raw_code) not in {str, int} or not str(raw_code):
            raise failure(
                "provider_result_unknown" if write else "provider_schema_unsupported"
            )
        code = str(raw_code)
        if code == "10001":
            raise failure("provider_session_expired")
        if code == "10005":
            raise failure("provider_application_forbidden")
        if code != "0000":
            raise failure("provider_rejected")
        if "data" not in body:
            raise failure(
                "provider_result_unknown" if write else "provider_schema_unsupported"
            )
        return body["data"]

    def discover_applications(self) -> list[JsonDict]:
        rows = self.post("/Oversea/App/getAppSwitchList", {"type": 1}, discover=True)
        if (
            not isinstance(rows, list)
            or not rows
            or any(not isinstance(row, dict) for row in rows)
        ):
            raise failure("provider_application_discovery_unverified")
        result = []
        seen = set()
        for row in rows:
            app_id = external_id(row.get("appid"))
            if app_id in seen:
                raise failure("provider_schema_unsupported")
            seen.add(app_id)
            app = JiashuClient(self.http, session=self.session, application_id=app_id)
            options = app.post(PREFIX + "getOptions", {"customer_id": ""})
            prefix = (
                options.get("channel_prefix") if isinstance(options, dict) else None
            )
            if not isinstance(prefix, str) or not prefix or prefix == "_":
                # No history-derived or old-account fallback prefix.
                raise failure("provider_channel_prefix_missing")
            result.append(
                {
                    "external_id": app_id,
                    "name": string(row.get("name")),
                    "channel_config": {"channel_prefix": prefix},
                    "tiktok_minis_id": None,
                }
            )
        return result

    def search(self, title: str, page: int) -> JsonDict:
        page = positive(page)
        data = self.post(
            "/Oversea/Video/getVideoList",
            {"keywords": title, "page": page, "page_size": 20},
        )
        rows, cursor = counted_page(data, page)
        items = [
            {
                "external_drama_id": external_id(row.get("video_id")),
                "title": string(row.get("name")),
                "language": row.get("language")
                if isinstance(row.get("language"), str)
                else None,
            }
            for row in rows
        ]
        return {"items": items, "next_cursor": cursor, "complete": cursor is None}

    def channel_for(self, drama_id: str) -> str:
        if not self.channel_prefix:
            raise failure("provider_channel_prefix_missing")
        return self.channel_prefix + external_id(drama_id)

    def find_existing(
        self, drama_id: str, config: JsonDict, cursor: str | None
    ) -> JsonDict:
        if set(config) - {"episode"}:
            raise failure("config_unverifiable")
        positive(config.get("episode", 1))
        channel = self.channel_for(drama_id)
        page = positive(cursor or 1)
        data = self.post(
            PREFIX + "getChannelList",
            {
                "page": page,
                "page_size": 20,
                "channel": channel,
                "customer_id": "",
                "remark": "",
            },
        )
        rows, next_cursor = counted_page(data, page)
        items = [
            {
                "remote_id": channel,
                "channel": channel,
                "remark": string(row.get("remark") or ""),
            }
            for row in rows
            if row.get("channel") == channel
        ]
        return {
            "items": items,
            "next_cursor": next_cursor,
            "complete": next_cursor is None,
        }

    def read_link(self, remote_id: str) -> JsonDict:
        data = self.post(PREFIX + "getGuideUrl", {"channel": remote_id})
        if not isinstance(data, dict) or not isinstance(data.get("config"), dict):
            raise failure("provider_schema_unsupported")
        config = data["config"]
        allowed = {
            key: config[key]
            for key in ("vid", "drama_num", "jump_url", "minis_path", "charge_level")
            if key in config
        }
        url = allowed.get("jump_url")
        if url is not None and not isinstance(url, str):
            raise failure("provider_schema_unsupported")
        attribution = {"channel": remote_id}
        if url:
            parsed = urlsplit(url)
            values = parse_qs(parsed.query, keep_blank_values=True)
            keys = ("channel", "vid", "dramaNum", "charge_level")
            if (
                parsed.scheme != "https"
                or parsed.hostname != "www.tiktok.com"
                or any(
                    len(values.get(key, [])) != 1 or not values[key][0] for key in keys
                )
            ):
                raise failure("config_unverifiable")
            attribution = {key: values[key][0] for key in keys}
            if (
                attribution["channel"] != remote_id
                or (
                    allowed.get("vid") is not None
                    and str(allowed["vid"]) != attribution["vid"]
                )
                or (
                    allowed.get("drama_num") is not None
                    and str(allowed["drama_num"]) != attribution["dramaNum"]
                )
            ):
                raise failure("config_conflict")
        return {
            "remote_id": remote_id,
            "url": url or None,
            "config": allowed,
            "protected_base": "",
            "attribution": attribution,
        }

    def create_step(self, step: str, payload: JsonDict) -> JsonDict:
        if step not in {"create", "generate", "save"}:
            raise failure("provider_request_invalid")
        vid = external_id(payload.get("vid"))
        channel = self.channel_for(vid)
        if payload.get("channel") != channel:
            raise failure("provider_request_invalid")
        if step == "create":
            body: JsonDict = {
                "channel": channel,
                "remark": string(payload.get("remark")),
                "customer_id": "",
            }
            data = self.post(PREFIX + "create", body, write=True)
        else:
            episode = positive(payload.get("drama_num"))
            existing = payload.get("existing_config")
            if not isinstance(existing, dict):
                raise failure("config_unverifiable")
            if existing.get("jump_url"):
                if existing.get("drama_num") is None:
                    raise failure("config_unverifiable")
                if positive(existing["drama_num"]) != episode or (
                    existing.get("vid") is not None and str(existing["vid"]) != vid
                ):
                    raise failure("config_conflict")
                # An existing URL is immutable here; orchestration must reuse it.
                raise failure("config_conflict")
            body = {"channel": channel, "vid": vid, "drama_num": episode}
            if step == "save":
                body.update(
                    jump_url=string(payload.get("jump_url")),
                    minis_path=string(payload.get("minis_path")),
                )
            data = self.post(
                PREFIX + ("generateGuideUrl" if step == "generate" else "saveGuideUrl"),
                body,
                write=True,
            )
        if step == "generate":
            if (
                not isinstance(data, dict)
                or not isinstance(data.get("url"), str)
                or not data["url"]
                or not isinstance(data.get("minis_path") or "", str)
            ):
                raise failure("provider_result_unknown")
            return {
                "url": data["url"],
                "minis_path": string(data.get("minis_path") or ""),
            }
        if data is not True and not (type(data) is int and data > 0):
            raise failure("provider_result_unknown")
        return {"accepted": True}
