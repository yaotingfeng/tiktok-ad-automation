"""只读公共元数据探测：不读取缓存、不注册、不授权、不刷新、不调用业务工具。"""

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2

from app.integrations.tiktok.mcp.protocol import (
    AUTHORIZATION_METADATA_URL,
    OFFICIAL_ENDPOINT,
    OFFICIAL_ISSUER,
    RESOURCE_METADATA_URL,
)

MAX_METADATA_BYTES = 128 * 1024
PUBLIC_FIELDS = frozenset(
    {
        "resource",
        "authorization_servers",
        "bearer_methods_supported",
        "scopes_supported",
        "issuer",
        "authorization_endpoint",
        "token_endpoint",
        "registration_endpoint",
        "revocation_endpoint",
        "code_challenge_methods_supported",
        "grant_types_supported",
        "response_types_supported",
        "token_endpoint_auth_methods_supported",
        "client_id_metadata_document_supported",
    }
)
METADATA_URLS = frozenset({RESOURCE_METADATA_URL, AUTHORIZATION_METADATA_URL})


async def fetch_public_metadata(url: str, *, transport: Any = None) -> dict[str, Any]:
    if url not in METADATA_URLS:
        raise ValueError("Only pinned public metadata URLs are allowed")
    # 不沿用代理/认证环境、cookie 或重定向；每次独立客户端，绝不携带上次 Set-Cookie。
    async with httpx2.AsyncClient(
        timeout=httpx2.Timeout(10, connect=5),
        follow_redirects=False,
        trust_env=False,
        transport=transport,
    ) as client:
        async with client.stream(
            "GET", url, headers={"Accept": "application/json"}
        ) as response:
            result: dict[str, Any] = {
                "source_url": url,
                "http_status": response.status_code,
            }
            if response.status_code != 200:
                return {
                    **result,
                    "evidence": "UNVERIFIED",
                    "reason": "metadata_http_status",
                }
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_METADATA_BYTES:
                    return {
                        **result,
                        "evidence": "UNVERIFIED",
                        "reason": "metadata_size_limit",
                    }
            try:
                raw = json.loads(body)
            except ValueError, UnicodeDecodeError:
                return {
                    **result,
                    "evidence": "UNVERIFIED",
                    "reason": "metadata_not_json",
                }
            if not isinstance(raw, dict):
                return {
                    **result,
                    "evidence": "UNVERIFIED",
                    "reason": "metadata_not_object",
                }
            if url == RESOURCE_METADATA_URL and (
                raw.get("resource") != OFFICIAL_ENDPOINT
                or raw.get("authorization_servers") != [OFFICIAL_ISSUER]
            ):
                return {
                    **result,
                    "evidence": "UNVERIFIED",
                    "reason": "resource_or_issuer_changed",
                }
            if (
                url == AUTHORIZATION_METADATA_URL
                and raw.get("issuer") != OFFICIAL_ISSUER
            ):
                return {**result, "evidence": "UNVERIFIED", "reason": "issuer_changed"}
            # 输出字段是公开协议白名单，不输出原始响应、headers、异常或任意额外字段。
            public = {key: value for key, value in raw.items() if key in PUBLIC_FIELDS}
            return {**result, "evidence": "PUBLIC_METADATA", "metadata": public}


async def inspect_metadata() -> dict[str, Any]:
    results = []
    for url in (RESOURCE_METADATA_URL, AUTHORIZATION_METADATA_URL):
        try:
            # 总时限还覆盖慢速分块响应；单次 connect/read 时限并不足够。
            async with asyncio.timeout(20):
                results.append(await fetch_public_metadata(url))
        except httpx2.HTTPError, TimeoutError:
            results.append(
                {
                    "source_url": url,
                    "evidence": "UNVERIFIED",
                    "reason": "metadata_transport_failed",
                }
            )
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "mode": "metadata-only",
        "endpoint": OFFICIAL_ENDPOINT,
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata-only", action="store_true", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(inspect_metadata())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
