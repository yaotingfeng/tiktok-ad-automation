"""冻结任务 JSON 的硬预算；截断保留显式路径/数量，不静默省略样本。"""

import json

from app.core.errors import DomainError

MAX_INPUT_BYTES = 512_000


def enforce_input_budget(evidence: dict, *, limit: int = MAX_INPUT_BYTES) -> dict:
    report = {
        "max_bytes": limit,
        "encoded_bytes": 0,
        "truncations": list(evidence.get("input_budget", {}).get("truncations", [])),
    }
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
                    if key == "reporting" and isinstance(child, list):
                        # 保留每个账户的三个窗口汇总，详细维度作为有标记的样本缩减。
                        details = [
                            row for row in child if row.get("dimension") != "account"
                        ]
                        if details:
                            found.append((size(details), value, key, f"{path}/{key}"))
                    found.extend(lists(child, f"{path}/{key}"))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                found.extend(lists(child, f"{path}/{index}"))
        return found

    # 每步严格减少列表元素，按实际输入收敛；多账户×成熟窗口不能受任意 200 步限制。
    while True:
        # encoded_bytes 自身也计入 JSON，迭代到位数稳定后再比较硬上限。
        for _ in range(3):
            report["encoded_bytes"] = size(evidence)
        if report["encoded_bytes"] <= limit:
            return evidence
        candidates = lists(evidence)
        if not candidates:
            raise DomainError(
                "ai_review_input_too_large", "复盘固定配置超过输入容量，请缩小配置"
            )
        _, parent, key, path = max(candidates, key=lambda item: item[0])
        count = len(parent[key])
        if key == "reporting":
            summaries = [
                row for row in parent[key] if row.get("dimension") == "account"
            ]
            details = [row for row in parent[key] if row.get("dimension") != "account"]
            parent[key] = summaries + details[: len(details) // 2]
        else:
            parent[key] = parent[key][: count // 2]
        parent["rows_truncated" if key == "rows" else f"{key}_truncated"] = True
        report["truncations"].append(
            {"path": path, "removed": count - len(parent[key])}
        )
