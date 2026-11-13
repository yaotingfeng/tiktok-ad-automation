"""纯输入预算回归，不使用数据库或外部服务。"""

import copy
import json

import pytest

from app.core.errors import ERROR_HTTP_STATUS, ERROR_PUBLIC_MESSAGES, DomainError
from app.modules.ai_workbench.input_budget import enforce_input_budget


def encoded_size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def test_many_nested_lists_can_converge_beyond_200_reductions():
    evidence = {"groups": [{"rows": ["x" * 1000]} for _ in range(250)]}
    result = enforce_input_budget(evidence, limit=25000)
    assert len(result["input_budget"]["truncations"]) > 200
    assert encoded_size(result) <= 25000
    assert result["input_budget"]["encoded_bytes"] == encoded_size(result)


def test_reporting_sampling_keeps_all_account_mature_windows_and_records_gaps():
    reports = [
        {
            "advertiser_id": str(account),
            "dimension": dimension,
            "maturity_horizon": horizon,
            "summary": {"spend": "100", "revenue": "120"},
            "rows": ["x" * 3000],
        }
        for account in range(10)
        for horizon in ("d0", "d3", "d7")
        for dimension in ("account", "campaign", "drama", "material")
    ]
    expected = {
        (r["advertiser_id"], r["maturity_horizon"])
        for r in reports
        if r["dimension"] == "account"
    }
    result = enforce_input_budget({"data_pack": {"reporting": reports}}, limit=18000)
    retained = result["data_pack"]["reporting"]
    assert {
        (r["advertiser_id"], r["maturity_horizon"])
        for r in retained
        if r["dimension"] == "account"
    } == expected
    assert all(r["summary"]["spend"] == "100" for r in retained)
    assert result["data_pack"]["reporting_truncated"] is True
    assert any(
        r["path"] == "/data_pack/reporting"
        for r in result["input_budget"]["truncations"]
    )
    assert encoded_size(result) <= 18000
    old = copy.deepcopy(result["input_budget"]["truncations"])
    assert enforce_input_budget(result)["input_budget"]["truncations"] == old


def test_fixed_input_overflow_is_an_actionable_public_error():
    with pytest.raises(DomainError) as exc:
        enforce_input_budget({"optimizer": {"fixed": "x" * 1000}}, limit=200)
    assert exc.value.code == "ai_review_input_too_large"
    assert ERROR_HTTP_STATUS[exc.value.code] == 422
    assert "复盘" in ERROR_PUBLIC_MESSAGES[exc.value.code]
