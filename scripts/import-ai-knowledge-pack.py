#!/usr/bin/env python3
"""通过显式环境的领域 API 幂等导入公共知识；凭据只从私有文件读取。"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("knowledge import refuses redirects")


def api_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("base URL must be a plain origin without credentials")
    if not parsed.hostname or parsed.scheme not in ("http", "https"):
        raise ValueError("invalid API origin")
    if parsed.scheme != "https" and parsed.hostname not in (
        "localhost",
        "127.0.0.1",
        "::1",
    ):
        raise ValueError("remote API requires HTTPS")
    return value.rstrip("/")


def same_payload(existing: dict, body: dict) -> bool:
    for key, value in body.items():
        # 共享审核是写入时的合规声明，读取接口不返回；不作为内容版本变化。
        if key == "sharing_review":
            continue
        previous = existing.get(key)
        if (
            key == "expires_at"
            and value
            and previous
            and datetime.fromisoformat(value) == datetime.fromisoformat(previous)
        ):
            continue
        if previous != value:
            return False
    return True


def import_pack(
    body: dict, *, origin: str, tenant_id: str, token: str, request=None
) -> dict:
    origin = api_origin(origin)
    tenant_id = str(UUID(tenant_id))
    if body.get("scope") != "PUBLIC":
        raise ValueError("this importer accepts reviewed PUBLIC packs only")
    if not token or "\n" in token or "\r" in token:
        raise ValueError("invalid bearer credential")
    opener = build_opener(NoRedirect())

    def send(method, path, payload=None):
        req = Request(
            origin + path,
            method=method,
            data=None
            if payload is None
            else json.dumps(payload, ensure_ascii=False).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        with opener.open(req, timeout=20) as response:
            return json.loads(response.read())

    transport = request or send
    base = f"/api/tenants/{tenant_id}/ai-knowledge"
    found = transport("GET", base + "?" + urlencode({"key": body["key"]}))
    existing = [
        row
        for row in found
        if row.get("scope") == "PUBLIC" and row.get("key") == body["key"]
    ]
    if len(existing) > 1:
        raise ValueError(
            "duplicate public knowledge assets require explicit reconciliation"
        )
    if existing and same_payload(existing[0], body):
        return {
            "status": "unchanged",
            **{k: existing[0][k] for k in ("id", "asset_id", "version")},
        }
    path = base if not existing else base + f"/{UUID(existing[0]['asset_id'])}/versions"
    result = transport("POST", path, body)
    return {
        "status": "updated" if existing else "imported",
        **{k: result[k] for k in ("id", "asset_id", "version")},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pack", type=Path)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--token-file", required=True, type=Path)
    args = parser.parse_args()
    try:
        origin = api_origin(args.base_url)
        body = json.loads(args.pack.read_text(encoding="utf-8"))
        token = args.token_file.read_text(encoding="utf-8").strip()
        result = import_pack(body, origin=origin, tenant_id=args.tenant_id, token=token)
    except (ValueError, OSError, HTTPError, URLError, KeyError):
        # HTTP 错误可能包含原始响应/请求，统一短消息避免输出私有数据或凭据。
        raise SystemExit(
            "Knowledge import failed; check origin, pack schema, admin permissions and response status"
        ) from None
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
