import base64
import hashlib
import hmac
import json

import httpx
import pytest

from app.core.errors import DomainError
from app.modules.automation_rules.feishu import (
    CardHit,
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


def test_card_shows_three_examples_and_links_to_the_rest():
    hits = [CardHit(f"对象{i}", "剧", [("消耗", "1.00")], 2) for i in range(12)]
    card = build_card(
        title="预警",
        rule_name="r",
        summary_lines=["命中 12"],
        hits=hits,
        total_hits=12,
        link="https://x/y",
    )
    text = json.dumps(card, ensure_ascii=False)
    assert "对象2" in text and "对象3" not in text
    assert "https://x/y" in text and "其余 9 个" in text
    assert len(text.encode()) < 20 * 1024


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


def test_long_names_cannot_expand_the_card_and_counts_remain_correct():
    long = "长" * 1500
    hits = [
        CardHit(label=f"{long}{i}", drama=None, metrics=[], target_count=1)
        for i in range(10)
    ]
    card = build_card(
        title="t",
        rule_name="r",
        summary_lines=[],
        hits=hits,
        total_hits=12,
        link="https://example.test",
    )
    assert len(json.dumps(card, ensure_ascii=False).encode()) < 20 * 1024
    content = card["card"]["elements"][1]["text"]["content"]
    assert long not in content
    assert len(content) < 250
    assert "其余 9 个请在 TK-ADA 查看" in content


def test_many_account_issues_are_summarized_without_hiding_missing_data():
    from app.modules.automation_rules.data_issues import input_issue_lines

    issues = [
        {
            "advertiser_id": f"account-{n}",
            "reason": "MATERIAL_LINK_MISSING",
            "count": "2",
        }
        for n in range(49)
    ]
    lines = input_issue_lines(issues + issues[:1])
    assert len(lines) == 1
    assert "98 个广告内素材" in lines[0] and "49 个账户" in lines[0]
    assert "素材与报表尚未关联" in lines[0]
    assert "account-" not in lines[0] and "账户数据未更新" not in lines[0]


def test_issue_summary_bounds_reason_lines_and_keeps_stale_accounts_distinct():
    from app.modules.automation_rules.data_issues import (
        MATERIAL_REASONS,
        input_issue_lines,
    )

    issues = [
        {"advertiser_id": "a", "reason": reason, "count": "1"}
        for reason in MATERIAL_REASONS
    ] + [{"advertiser_id": "stale", "reason": "STALE_REPORT"}]
    lines = input_issue_lines(issues)
    assert len(lines) == 4
    assert lines[0] == "1 个账户数据未更新，已跳过"
    assert lines[-1] == "另有 5 类缺数问题，详见运行记录"
