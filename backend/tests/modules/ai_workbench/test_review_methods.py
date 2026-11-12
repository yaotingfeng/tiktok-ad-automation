from hashlib import sha256


def test_methods_freeze_real_bodies_and_report_missing_inputs():
    from app.modules.ai_workbench.methods import freeze_methods

    frozen = freeze_methods(
        {
            "plans": [],
            "rule_evidence": [],
            "data_pack": {"reporting": [], "business": []},
        }
    )
    assert frozen["main"]["body"]
    assert (
        frozen["main"]["sha256"] == sha256(frozen["main"]["body"].encode()).hexdigest()
    )
    assert {m["key"] for m in frozen["references"]} == {
        "builds",
        "rules",
        "materials",
        "business",
        "scale",
    }
    assert all(
        m["required_evidence"] and m["status"] == "INSUFFICIENT_DATA"
        for m in frozen["references"]
    )
    assert frozen["main"]["version"]
    assert frozen["references"][-1]["execution"] == "ADVISORY"
