"""冻结随应用发布的方法正文，历史任务不重新读取磁盘。"""

from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).with_name("method_resources") / "ai-delivery-review"
VERSION = "1.0.0"
REQUIRED = {
    "builds": ["plans", "build_occurrences", "build_object_mapping"],
    "rules": ["rule_evidence", "readback_status"],
    "materials": ["material_report", "production_strategy_mapping"],
    "business": ["published_business_days"],
    "scale": [
        "complete_day_reports",
        "mature_d3_d7",
        "account_capacity",
        "material_supply",
    ],
}


def _resource(key, path):
    body = path.read_text(encoding="utf-8")
    return {
        "key": key,
        "version": VERSION,
        "sha256": sha256(body.encode()).hexdigest(),
        "body": body,
    }


def freeze_methods(evidence: dict) -> dict:
    pack = evidence.get("data_pack", {})
    available = {
        "builds": bool(evidence.get("build_occurrences")),
        "rules": bool(evidence.get("rule_evidence")),
        "materials": any(
            r.get("dimension") == "material" and r.get("rows")
            for r in pack.get("reporting", [])
        ),
        "business": any(r.get("rows") for r in pack.get("business", [])),
        "scale": any(r.get("rows") for r in pack.get("reporting", [])),
    }
    return {
        "main": _resource("ai-delivery-review", ROOT / "SKILL.md"),
        "references": [
            {
                **_resource(key, ROOT / "references" / f"{key}.md"),
                "status": "PARTIAL_EVIDENCE" if available[key] else "INSUFFICIENT_DATA",
                "required_evidence": required,
                "execution": "ADVISORY" if key == "scale" else "DOMAIN_PROPOSAL_ONLY",
            }
            for key, required in REQUIRED.items()
        ],
    }
