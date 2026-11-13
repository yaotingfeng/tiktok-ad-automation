#!/usr/bin/env python3
"""Import a reviewed TK-ADA AI knowledge pack through the tenant API.

The script intentionally requires an explicit tenant, base URL and bearer token;
it never guesses a tenant/BC and never prints the token.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pack", type=Path)
    parser.add_argument("--base-url", required=True, help="TK-ADA API origin")
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--token", required=True)
    args = parser.parse_args()
    body = json.loads(args.pack.read_text(encoding="utf-8"))
    if body.get("scope") != "PUBLIC":
        raise SystemExit("This pack is intentionally limited to PUBLIC knowledge")
    url = args.base_url.rstrip("/") + f"/api/tenants/{args.tenant_id}/ai-knowledge"
    request = Request(url, method="POST", data=json.dumps(body, ensure_ascii=False).encode())
    request.add_header("Authorization", f"Bearer {args.token}")
    request.add_header("Content-Type", "application/json")
    try:
        with urlopen(request, timeout=20) as response:
            result = json.loads(response.read())
    except (HTTPError, URLError) as error:
        raise SystemExit(f"TK-ADA knowledge import failed: {error}") from error
    print(json.dumps({"status": "imported", "id": result.get("id"), "asset_id": result.get("asset_id"), "version": result.get("version")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
