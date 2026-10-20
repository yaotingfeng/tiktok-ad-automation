"""素材工具 6.9/6.10 合同；所有上游正文与传输异常均不向调用方透传。"""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx

ORIGIN = "https://manjumt.gzjunbo.net"
KNOWN_ERRORS = {
    "INVALID_PROVISIONING_KEY": "素材工具开户密钥无效，请联系平台管理员",
    "PROVISIONING_REQUEST_INVALID": "素材工具拒绝开户参数，请联系平台管理员",
    "COMPANY_NAME_RESERVED": "素材工具不允许使用此公司名称",
    "WEAK_PASSWORD": "初始密码不符合素材工具要求",
    "USERNAME_EXISTS": "素材账号已存在，请联系平台管理员核对",
    "IDEMPOTENCY_CONFLICT": "素材工具开户内容与原操作不一致",
    "ACCOUNT_NOT_FOUND": "素材账号不可用，或不是当前开户密钥创建的账号",
    "LOGIN_LINK_REQUEST_INVALID": "素材工具拒绝登录请求，请联系平台管理员",
    "API_KEY_ROUTE_FORBIDDEN": "素材工具未授权此接口，请联系平台管理员",
    "UNAVAILABLE": "素材工具暂时不可用，请稍后重试",
    "INVALID_RESPONSE": "素材工具返回内容不符合接口约定",
    "REQUEST_REJECTED": "素材工具拒绝请求，请联系平台管理员",
}


class ToolError(Exception):
    def __init__(self, code: str, *, retryable: bool = False):
        self.code = code if code in KNOWN_ERRORS else "REQUEST_REJECTED"
        self.retryable = retryable
        super().__init__(KNOWN_ERRORS[self.code])


@dataclass(frozen=True)
class Provisioned:
    company_id: str
    user_id: str
    username: str
    api_key_id: str
    api_key: str = field(repr=False)


@dataclass(frozen=True)
class LoginLink:
    login_url: str = field(repr=False)
    expires_at: datetime


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,255}", value):
        raise ValueError
    return value


class MaterialToolClient:
    def __init__(self, key: str, *, transport: httpx.BaseTransport | None = None):
        self._key = key
        self._transport = transport

    def _post(
        self, path: str, body: dict[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._key}"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            # 每次使用独立无 Cookie 客户端，密钥绝不随重定向发送到其他地址。
            with httpx.Client(
                timeout=httpx.Timeout(15, connect=5),
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                response = client.post(ORIGIN + path, headers=headers, json=body)
        except httpx.HTTPError:
            raise ToolError("UNAVAILABLE", retryable=True) from None
        if response.status_code == 429 or response.status_code >= 500:
            raise ToolError("UNAVAILABLE", retryable=True)
        try:
            data = response.json()
        except ValueError:
            raise ToolError("INVALID_RESPONSE", retryable=response.is_success) from None
        if response.status_code not in {200, 201}:
            code = data.get("error") if isinstance(data, dict) else None
            raise ToolError(code if isinstance(code, str) else "REQUEST_REJECTED")
        if not isinstance(data, dict):
            raise ToolError("INVALID_RESPONSE", retryable=True)
        return data

    def provision(self, body: dict[str, Any], *, idempotency_key: str) -> Provisioned:
        data = self._post(
            "/api/provisioning/users", body, idempotency_key=idempotency_key
        )
        try:
            company, user, key = data["company"], data["user"], data["apiKey"]
            company_id = _identifier(company["id"])
            if (
                company["name"] != body["companyName"]
                or company["deliverySettings"]["uploadPlatform"] != "r2"
                or company["deliverySettings"]["pushAdPlatform"] is not True
                or user["username"] != body["username"]
                or key["companyId"] != company_id
                or key["kind"] != "company"
                or not isinstance(data["key"], str)
                or not re.fullmatch(r"owk_[A-Za-z0-9_-]+", data["key"])
            ):
                raise ValueError
            return Provisioned(
                company_id=company_id,
                user_id=_identifier(user["id"]),
                username=user["username"],
                api_key_id=_identifier(key["id"]),
                api_key=data["key"],
            )
        except KeyError, TypeError, ValueError:
            # 可能已开户成功；仍需相同幂等键恢复，不能改为新单。
            raise ToolError("INVALID_RESPONSE", retryable=True) from None

    def login_link(self, username: str) -> LoginLink:
        data = self._post("/api/provisioning/login-links", {"username": username})
        try:
            path = data["loginUrl"]
            if not isinstance(path, str) or any(ord(c) < 33 for c in path):
                raise ValueError
            parsed = urlsplit(path)
            query = parse_qsl(parsed.query, keep_blank_values=True)
            # 合同只接受相对 consume 地址，拒绝外域、重定向字段和重复 token。
            if (
                parsed.scheme
                or parsed.netloc
                or parsed.fragment
                or parsed.path != "/api/login-links/consume"
                or len(query) != 1
                or query[0][0] != "token"
                or not 1 <= len(query[0][1]) <= 4096
                or any(ord(c) < 33 or ord(c) == 127 for c in query[0][1])
            ):
                raise ValueError
            expires = datetime.fromisoformat(data["expiresAt"])
            if (
                expires.tzinfo is None
                or not 0 < (expires - datetime.now(UTC)).total_seconds() <= 360
            ):
                raise ValueError
            return LoginLink(login_url=ORIGIN + path, expires_at=expires)
        except KeyError, TypeError, ValueError:
            raise ToolError("INVALID_RESPONSE") from None
