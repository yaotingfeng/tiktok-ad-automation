"""租户飞书群自定义机器人:只允许官方 Webhook 域名,不跟随重定向。"""

import base64
import hashlib
import hmac
import re
import time
from typing import Any

import httpx

from app.core.errors import DomainError

_HOOK = re.compile(
    r"^https://open\.(feishu\.cn|larksuite\.com)/open-apis/bot/v2/hook/[A-Za-z0-9-]+$"
)


def validate_webhook(url: str) -> str:
    value = (url or "").strip()
    if not _HOOK.fullmatch(value):
        raise DomainError("feishu_webhook_invalid", "请填写飞书群机器人的 Webhook 地址")
    return value


def url_hint(url: str) -> str:
    token = url.rsplit("/", 1)[-1]
    return f"{url[: -len(token)]}…{token[-4:]}"


def sign(secret: str, timestamp: int) -> str:
    # 飞书签名:以 "timestamp\nsecret" 为密钥对空消息做 HmacSHA256 后 Base64。
    key = f"{timestamp}\n{secret}".encode()
    return base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode()


def compact_text(value: str, limit: int) -> str:
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def rule_caption(name: str) -> str:
    # 条件、阈值和对象明细放工具里；通知标题只保留可识别的规则名。
    return compact_text(name.split("｜", 1)[0].split("|", 1)[0], 18)


def build_card(
    *,
    title: str,
    text: str,
    link: str,
    template: str = "orange",
    button: str = "查看详情",
) -> dict[str, Any]:
    """通知固定为短标题、一句结果和按钮，不在消息里展开报表。"""
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": False},
            "header": {
                "template": template,
                "title": {"tag": "plain_text", "content": compact_text(title, 24)},
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "plain_text", "content": compact_text(text, 48)},
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "type": "primary",
                            "url": link,
                            "text": {"tag": "plain_text", "content": button},
                        }
                    ],
                },
            ],
        },
    }


def send(
    url: str,
    secret: str,
    card: dict[str, Any],
    *,
    transport: httpx.BaseTransport | None = None,
    now: float | None = None,
) -> None:
    body = dict(card)
    if secret:
        timestamp = int(now if now is not None else time.time())
        body.update(timestamp=str(timestamp), sign=sign(secret, timestamp))
    try:
        with httpx.Client(
            transport=transport, timeout=10, follow_redirects=False
        ) as client:
            response = client.post(validate_webhook(url), json=body)
    except httpx.HTTPError:
        raise DomainError("feishu_send_failed", "飞书发送失败:网络错误") from None
    if response.status_code != 200:
        raise DomainError(
            "feishu_send_failed", f"飞书发送失败:HTTP {response.status_code}"
        )
    try:
        data = response.json()
    except ValueError:
        raise DomainError("feishu_send_failed", "飞书发送失败:响应无效") from None
    code = data.get("code", data.get("StatusCode"))
    if code != 0:
        raise DomainError(
            "feishu_send_failed", f"飞书发送失败:{data.get('msg') or code}"
        )
