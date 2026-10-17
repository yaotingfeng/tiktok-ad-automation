import base64
import hashlib
import hmac
import json

import httpx
import pytest

from app.core.errors import DomainError
from app.modules.automation_rules.feishu import (
    build_card,
    send,
    sign,
    url_hint,
    validate_webhook,
)

HOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/abc-123"


@pytest.mark.parametrize(
    "url",
    [
        "http://open.feishu.cn/open-apis/bot/v2/hook/abc",
        "https://open.feishu.cn.evil.com/open-apis/bot/v2/hook/abc",
        "https://evil.com/?u=https://open.feishu.cn/open-apis/bot/v2/hook/abc",
        "https://open.feishu.cn/open-apis/bot/v2/hook/",
        "https://user@open.feishu.cn/open-apis/bot/v2/hook/abc",
        "https://open.feishu.cn:8443/open-apis/bot/v2/hook/abc",
    ],
)
def test_rejects_non_feishu_hosts(url):
    with pytest.raises(DomainError):
        validate_webhook(url)


def test_accepts_feishu_and_lark():
    assert validate_webhook(f" {HOOK} ") == HOOK
    validate_webhook("https://open.larksuite.com/open-apis/bot/v2/hook/x1")


def test_hint_hides_token():
    assert "abc-123" not in url_hint(HOOK)


def test_sign_matches_feishu_algorithm():
    expected = base64.b64encode(
        hmac.new(b"1700000000\nsec", b"", hashlib.sha256).digest()
    ).decode()
    assert sign("sec", 1700000000) == expected


def test_notification_is_one_result_and_a_details_button():
    card = build_card(title="当日止损", text="暂停已提交 1", link="https://x/y")
    elements = card["card"]["elements"]
    assert len(elements) == 2
    assert elements[0]["text"]["content"] == "暂停已提交 1"
    assert elements[1]["actions"][0]["url"] == "https://x/y"
    assert elements[1]["actions"][0]["text"]["content"] == "查看详情"


def test_send_posts_signed_body_without_redirects():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"code": 0})

    send(
        HOOK,
        "sec",
        {"msg_type": "interactive", "card": {}},
        transport=httpx.MockTransport(handler),
        now=1700000000,
    )
    assert seen["body"]["timestamp"] == "1700000000"
    assert seen["body"]["sign"] == sign("sec", 1700000000)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"code": 19021, "msg": "sign match fail"}),
        httpx.Response(302, headers={"location": "https://evil.com"}),
        httpx.Response(500),
    ],
)
def test_send_failures_raise(response):
    with pytest.raises(DomainError):
        send(
            HOOK,
            "",
            {"msg_type": "interactive", "card": {}},
            transport=httpx.MockTransport(lambda r: response),
        )


def test_long_text_cannot_turn_a_notification_into_a_report():
    card = build_card(
        title="长名称" * 1000, text="很多文字\n" * 1000, link="https://x/y"
    )
    title = card["card"]["header"]["title"]["content"]
    body = card["card"]["elements"][0]["text"]["content"]
    assert len(title) + len(body) <= 72
    assert "\n" not in body
    assert len(card["card"]["elements"]) == 2


def test_only_normal_waiting_for_first_report_is_quiet():
    from app.modules.automation_rules.data_issues import actionable_input_issues

    waiting = {"advertiser_id": "a", "reason": "MATERIAL_LINK_MISSING", "count": "29"}
    stale = {"advertiser_id": "b", "reason": "STALE_REPORT"}
    missing_parent = {
        "advertiser_id": "c",
        "reason": "MATERIAL_PARENT_UNKNOWN",
        "count": "1",
    }
    assert actionable_input_issues([waiting, stale, missing_parent]) == [
        stale,
        missing_parent,
    ]
