"""冻结任务 JSON 的硬预算；截断保留显式路径/数量，不静默省略样本。"""

import json

from app.core.errors import DomainError

MAX_INPUT_BYTES = 512_000


def enforce_input_budget(evidence: dict, *, limit: int = MAX_INPUT_BYTES) -> dict:
    report = {"max_bytes": limit, "truncations": []}
    evidence["input_budget"] = report

    def size(value):
        return len(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        )

    def lists(value, path=""):
        found = []
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {
                    "methods",
                    "capabilities",
                    "input_budget",
                    "optimizer",
                    "strategy",
                    "rules",
                    "knowledge",
                }:
                    continue
                if (
                    isinstance(child, list)
                    and child
                    and key
                    in {
                        "rows",
                        "rule_evidence",
                        "build_occurrences",
                        "plans",
                        "proposals",
                        "reviews",
                        "coverage",
                        "exports",
                    }
                ):
                    found.append((size(child), value, key, f"{path}/{key}"))
                else:
                    found.extend(lists(child, f"{path}/{key}"))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                found.extend(lists(child, f"{path}/{index}"))
        return found

    for _ in range(200):
        if size(evidence) <= limit:
            report["encoded_bytes"] = size(evidence) + 40
            return evidence
        candidates = lists(evidence)
        if not candidates:
            raise DomainError(
                "ai_review_input_too_large", "复盘固定配置超过输入容量，请缩小配置"
            )
        _, parent, key, path = max(candidates, key=lambda item: item[0])
        count = len(parent[key])
        parent[key] = parent[key][: count // 2]
        parent["rows_truncated" if key == "rows" else f"{key}_truncated"] = True
        report["truncations"].append(
            {"path": path, "removed": count - len(parent[key])}
        )
    raise DomainError("ai_review_input_too_large", "复盘输入超过容量")
