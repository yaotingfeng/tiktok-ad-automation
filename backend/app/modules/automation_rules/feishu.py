"""租户飞书群自定义机器人:只允许官方 Webhook 域名,不跟随重定向。"""

import base64
import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.errors import DomainError

_HOOK = re.compile(
    r"^https://open\.(feishu\.cn|larksuite\.com)/open-apis/bot/v2/hook/[A-Za-z0-9-]+$"
)
MAX_CARD_HITS = 10
MAX_CARD_BYTES = 20 * 1024


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


@dataclass(frozen=True)
class CardHit:
    label: str
    drama: str | None
    metrics: list[tuple[str, str]]
    target_count: int


def _hit_line(hit: CardHit) -> str:
    metrics = ",".join(f"{name} {value}" for name, value in hit.metrics)
    drama = f"【{hit.drama}】" if hit.drama else ""
    return f"- {drama}{hit.label}:{metrics}(涉及 {hit.target_count} 个对象)"


def build_card(
    *,
    title: str,
    rule_name: str,
    summary_lines: list[str],
    hits: list[CardHit],
    total_hits: int,
    link: str,
    template: str = "orange",
    button: str = "查看明细",
) -> dict[str, Any]:
    lines = [_hit_line(hit) for hit in hits[:MAX_CARD_HITS]]
    if total_hits > len(lines):
        lines.append(f"其余 {total_hits - len(lines)} 个请在 TK-ADA 查看")
    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**规则**:{rule_name}\n" + "\n".join(summary_lines),
            },
        },
    ]
    if lines:
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}
        )
    elements.append(
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
        }
    )
    card = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": template,
                "title": {"tag": "plain_text", "content": title},
            },
            "elements": elements,
        },
    }
    # 极端长名称下逐条减少明细,保证不超过飞书卡片体积上限。
    while (
        len(json.dumps(card, ensure_ascii=False).encode()) >= MAX_CARD_BYTES and lines
    ):
        lines.pop(-2 if len(lines) > 1 else -1)
        elements[1]["text"]["content"] = "\n".join(lines)
    return card


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
