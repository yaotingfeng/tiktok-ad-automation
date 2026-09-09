"""Source-evidenced Wangyan protocol; real service acceptance remains separate."""

import base64
import binascii
import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from http.cookies import SimpleCookie
from urllib.parse import urlsplit

import httpx

from app.core.errors import DomainError

from .contract import JsonDict, external_id, failure, positive, request_json, string

BASE = "https://partners.shortswave.com"
PAGE_SIZE = 20
# Engineering bound, not a provider limit or proof that truncated history is full.
MAX_HISTORY_PAGES = 10_000
HISTORY_START = "1970-01-01"


def _tomorrow() -> str:
    return (datetime.now(UTC).date() + timedelta(days=1)).isoformat()


def _scope(app: str, drama: str, episode: int) -> str:
    return hashlib.sha256(
        json.dumps([app, drama, episode], separators=(",", ":")).encode()
    ).hexdigest()


def _cursor(value: str | None, *, scope: str) -> JsonDict:
    if value is None:
        return {
            "v": 1,
            "scope": scope,
            "start": HISTORY_START,
            "end": _tomorrow(),
            "page": 1,
            "total": None,
        }
    try:
        if not isinstance(value, str) or len(value) > 1024:
            raise ValueError
        result = json.loads(base64.b64decode(value, altchars=b"-_", validate=True))
        if not isinstance(result, dict) or set(result) != {
            "v",
            "scope",
            "start",
            "end",
            "page",
            "total",
        }:
            raise ValueError
        if (
            type(result["v"]) is not int
            or result["v"] != 1
            or result["scope"] != scope
            or result["start"] != HISTORY_START
            or not isinstance(result["end"], str)
            or date.fromisoformat(result["end"]).isoformat() != result["end"]
            or not HISTORY_START <= result["end"] <= _tomorrow()
            or type(result["page"]) is not int
            or not 2 <= result["page"] <= MAX_HISTORY_PAGES
            or type(result["total"]) is not int
            or not 0 < result["total"] <= MAX_HISTORY_PAGES * PAGE_SIZE
            or (result["page"] - 1) * PAGE_SIZE >= result["total"]
        ):
            raise ValueError
        return result
    except ValueError, TypeError, KeyError, binascii.Error, UnicodeError:
        raise failure("provider_request_invalid") from None


def _response_id(value: object) -> str:
    try:
        return str(positive(value))
    except DomainError:
        raise failure("provider_schema_unsupported") from None


def _comparable(row: JsonDict) -> tuple[str, str, str, int]:
    try:
        app, drama, platform = (
            external_id(row.get("app")),
            external_id(row.get("drama_id")),
            string(row.get("promote_platform")),
        )
        episode = positive(row.get("chapter_index"))
        if not platform:
            raise ValueError
        return app, drama, platform, episode
    except ValueError, DomainError:
        raise failure("config_unverifiable") from None


def _link(row: JsonDict) -> JsonDict:
    app, drama, platform, episode = _comparable(row)
    identity, numeric_drama = (
        _response_id(row.get("id")),
        positive(row.get("drama_int_id")),
    )
    url = row.get("tt_minis_link")
    if url is not None:
        if not isinstance(url, str):
            raise failure("config_unverifiable")
        if url:
            try:
                parsed = urlsplit(url)
            except ValueError:
                raise failure("config_unverifiable") from None
            if (
                parsed.scheme != "https"
                or parsed.hostname != "www.tiktok.com"
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise failure("config_unverifiable")
    name, protected = row.get("promote_name"), row.get("campaign_name")
    if (name is not None and not isinstance(name, str)) or (
        protected is not None and not isinstance(protected, str)
    ):
        raise failure("config_unverifiable")
    return {
        "remote_id": identity,
        "url": url or None,
        "config": {"vid": drama, "drama_num": episode, "jump_url": url or None},
        "promote_name": name,
        "protected_base": protected or None,
        "attribution": {
            "id": int(identity),
            "drama_int_id": numeric_drama,
            "chapter_index": episode,
            "app": app,
            "drama_id": drama,
            "promote_platform": platform,
            "campaign_name": protected or None,
        },
    }


class WangyanClient:
    def __init__(self, http: httpx.Client, *, token: str, application_id: str):
        self.http, self.token, self.application_id = http, token, application_id

    @classmethod
    def login(
        cls, http: httpx.Client, *, email: str, password: str, application_id: str = ""
    ) -> WangyanClient:
        body, response = request_json(
            http,
            "POST",
            BASE + "/api/login/pwd_login",
            headers={"cookie": ""},
            json={"email": email, "password": password},
        )
        if type(body.get("code")) is not int or body["code"] != 0:
            raise failure("provider_session_expired")
        cookies = SimpleCookie()
        for cookie in response.headers.get_list("set-cookie"):
            cookies.load(cookie)
        token = cookies.get("x-ds-admin-token")
        if not token or not token.value:
            raise failure("provider_session_expired")
        return cls(http, token=token.value, application_id=application_id)

    def search(self, title: str, page: int) -> JsonDict:
        page = positive(page)
        body, _ = request_json(
            self.http,
            "GET",
            BASE + "/api/distribute_admin/drama/list",
            headers={"cookie": "x-ds-admin-token=" + self.token},
            params={
                "app": self.application_id,
                "page": page,
                "page_size": 20,
                "title": title,
            },
        )
        if type(body.get("code")) is not int or body["code"] != 0:
            raise failure("provider_rejected")
        rows = body.get("data")
        if (
            not isinstance(rows, list)
            or any(not isinstance(row, dict) for row in rows)
            or len(rows) > 20
        ):
            raise failure("provider_schema_unsupported")
        items = [
            {
                "external_drama_id": external_id(row.get("id")),
                "title": string(row.get("title")),
                "language": row.get("lang")
                if isinstance(row.get("lang"), str)
                else None,
            }
            for row in rows
        ]
        # No total is returned by the CLI contract. Continue until an empty page;
        # a short nonempty page alone never asserts complete enumeration.
        return {
            "items": items,
            "next_cursor": str(page + 1) if rows else None,
            "complete": not rows,
        }

    def discover_applications(self) -> list[JsonDict]:
        body, _ = request_json(
            self.http,
            "GET",
            BASE + "/api/account/group/apps",
            headers={"cookie": "x-ds-admin-token=" + self.token},
        )
        if type(body.get("code")) is not int or body["code"] != 0:
            raise failure("provider_rejected")
        rows = body.get("data")
        if (
            not isinstance(rows, list)
            or not rows
            or any(not isinstance(row, dict) for row in rows)
        ):
            raise failure("provider_application_discovery_unverified")
        items, seen = [], set()
        for row in rows:
            app = external_id(row.get("package_name"))
            if app in seen or row.get("is_tt") not in (0, 1, "0", "1"):
                raise failure("provider_schema_unsupported")
            seen.add(app)
            items.append(
                {
                    "external_id": app,
                    "name": string(row.get("name")),
                    "channel_config": {"is_tt": str(row["is_tt"]) == "1"},
                    "tiktok_minis_id": None,
                }
            )
        return items

    def _list(
        self, *, page: int, start: str, end: str, **filters: str
    ) -> tuple[list[JsonDict], int, list[str]]:
        if not self.application_id.strip():
            raise failure("provider_request_invalid")
        body, _ = request_json(
            self.http,
            "GET",
            BASE + "/api/distribute_admin/promote/link/list",
            headers={"cookie": "x-ds-admin-token=" + self.token},
            params={
                "app": self.application_id,
                "page": page,
                "page_size": PAGE_SIZE,
                "start": start,
                "end": end,
                **filters,
            },
        )
        if type(body.get("code")) is not int:
            raise failure("provider_schema_unsupported")
        if body["code"] != 0:
            raise failure("provider_rejected")
        rows, total = body.get("data"), body.get("total")
        if (
            not isinstance(rows, list)
            or any(not isinstance(row, dict) for row in rows)
            or len(rows) > PAGE_SIZE
            or type(total) is not int
            or total < 0
        ):
            raise failure("provider_schema_unsupported")
        if total > MAX_HISTORY_PAGES * PAGE_SIZE:
            raise failure("lookup_incomplete")
        offset = (page - 1) * PAGE_SIZE
        if len(rows) != min(PAGE_SIZE, max(0, total - offset)) or offset > total:
            raise failure("lookup_incomplete")
        identities = [_response_id(row.get("id")) for row in rows]
        if len(set(identities)) != len(identities):
            raise failure("lookup_incomplete")
        return rows, total, identities

    def find_existing(
        self, drama_id: str, config: JsonDict, cursor: str | None
    ) -> JsonDict:
        if set(config) - {"episode"}:
            raise failure("config_unverifiable")
        drama_id, episode = external_id(drama_id), positive(config.get("episode", 1))
        state = _cursor(cursor, scope=_scope(self.application_id, drama_id, episode))
        rows, total, identities = self._list(
            page=state["page"],
            start=state["start"],
            end=state["end"],
            drama_id=drama_id,
        )
        if state["total"] is not None and state["total"] != total:
            raise failure("lookup_incomplete")
        items = []
        for row in rows:
            if _comparable(row) == (self.application_id, drama_id, "tiktok", episode):
                items.append(_link(row))
        complete = state["page"] * PAGE_SIZE >= total
        next_cursor = None
        if not complete:
            state.update(page=state["page"] + 1, total=total)
            next_cursor = base64.urlsafe_b64encode(
                json.dumps(state, separators=(",", ":"), sort_keys=True).encode()
            ).decode()
        # The workflow persists all observed IDs and rejects cross-page repeats;
        # the cursor remains constant-size even for a large history.
        return {
            "items": items,
            "next_cursor": next_cursor,
            "complete": complete,
            "total": total,
            "observed_ids": identities,
        }

    def read_link(self, remote_id: str) -> JsonDict:
        remote_id = str(positive(remote_id))
        rows, total, identities = self._list(
            page=1, start=HISTORY_START, end=_tomorrow(), id=remote_id
        )
        if total != 1 or identities != [remote_id]:
            # An absent read is not proof that an unknown POST had no effect.
            raise failure("lookup_incomplete")
        app, _, platform, _ = _comparable(rows[0])
        if app != self.application_id or platform != "tiktok":
            raise failure("config_conflict")
        return _link(rows[0])

    def create_step(self, step: str, payload: JsonDict) -> JsonDict:
        if step != "create" or set(payload) - {"vid", "drama_num", "promote_name"}:
            raise failure("provider_request_invalid")
        if not self.application_id.strip():
            raise failure("provider_request_invalid")
        body: JsonDict = {
            "app": self.application_id,
            "drama_id": external_id(payload.get("vid")),
            "chapter_index": positive(payload.get("drama_num")),
            "promote_platform": "tiktok",
        }
        if "promote_name" in payload:
            name = payload["promote_name"]
            if not isinstance(name, str) or not name.strip() or len(name) > 255:
                raise failure("provider_request_invalid")
            body["promote_name"] = name
        result, _ = request_json(
            self.http,
            "POST",
            BASE + "/api/distribute_admin/promote/link/create",
            headers={"cookie": "x-ds-admin-token=" + self.token},
            json=body,
            write=True,
        )
        if type(result.get("code")) is not int:
            raise failure("provider_result_unknown")
        if result["code"] != 0:
            raise failure("provider_rejected")
        data = result.get("data")
        if data is not None and not isinstance(data, dict):
            raise failure("provider_result_unknown")
        receipt: JsonDict = {"accepted": True}
        if isinstance(data, dict) and "id" in data:
            try:
                receipt["remote_id"] = str(positive(data["id"]))
            except DomainError:
                raise failure("provider_result_unknown") from None
        return receipt


def render_attribution(row: JsonDict, title: str) -> str:
    """Pinned public LinkAdvertModal source; evidence URL/hash in integration docs."""
    original = row.get("campaign_name")
    if isinstance(original, str) and original:
        return original
    try:
        drama = positive(row.get("drama_int_id"))
        remote = positive(row.get("id"))
        chapter = positive(row.get("chapter_index"))
        if not isinstance(title, str) or not title:
            raise ValueError
    except ValueError, DomainError:
        raise failure("attribution_contract_unverified") from None
    return f"{{b{drama}/s{remote}/c{chapter}}}-{title}"
