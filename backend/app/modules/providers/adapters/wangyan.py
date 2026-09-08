"""Wangyan verified request shapes; unknown production contracts stay blocked."""

from http.cookies import SimpleCookie

import httpx

from app.core.errors import DomainError

from .contract import JsonDict, external_id, failure, positive, request_json, string

BASE = "https://partners.shortswave.com"


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

    def find_existing(
        self, drama_id: str, config: JsonDict, cursor: str | None
    ) -> JsonDict:
        # The only known listing defaults to 30 days; absence is not proven.
        raise failure("lookup_incomplete")

    def read_link(self, remote_id: str) -> JsonDict:
        # No exact-ID read contract or complete-history query has been evidenced.
        raise failure("lookup_incomplete")

    def create_step(self, step: str, payload: JsonDict) -> JsonDict:
        # No caller boolean can bypass this production gate. A future adapter
        # revision requires reviewed historical lookup and attribution evidence.
        raise failure("lookup_incomplete")


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
