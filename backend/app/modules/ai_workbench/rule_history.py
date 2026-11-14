"""规则判断时事实的冻结与确定性重放；不推断历史收入或干预因果。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from decimal import Decimal
from typing import Any

from app.modules.automation_rules.catalog import RULE_METRICS
from app.modules.automation_rules.evaluate import evaluate
from app.modules.automation_rules.inputs import (
    AccountInfo,
    Entity,
    Inputs,
    in_rule_scope,
)
from app.modules.automation_rules.schemas import RuleDefinition
from app.modules.reporting.metric_catalog import DERIVED_RATIOS

SNAPSHOT_VERSION = 1
_RATIOS = {name: (num, den) for name, num, den, _ in DERIVED_RATIOS}


def _json_copy(value: Any) -> Any:
    return json.loads(
        json.dumps(
            value,
            default=lambda item: (
                str(item) if isinstance(item, Decimal) else _unsupported(item)
            ),
        )
    )


def _unsupported(value: Any) -> Any:
    raise TypeError(f"Unsupported snapshot value: {type(value).__name__}")


def _digest(objects: Any, facts: Any, accounts: Any, skipped: Any) -> str:
    payload = json.dumps(
        [objects, facts, accounts, skipped], sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def freeze_rule_inputs(inputs: Inputs) -> dict[str, Any]:
    """保留全部输入层级，含未命中、父子链和素材使用位置；不只保存执行目标。"""
    objects = []
    for entity in sorted(inputs.entities.values(), key=lambda item: item.key):
        item = _json_copy(asdict(entity))
        item["decimal_attributes"] = sorted(
            key
            for key, value in entity.attributes.items()
            if isinstance(value, Decimal)
        )
        # 执行审计仍通过 before 读取预算、状态等运行前值。
        item["before"] = dict(item["attributes"])
        objects.append(item)
    facts = _json_copy(inputs.facts)
    accounts = {key: asdict(value) for key, value in sorted(inputs.accounts.items())}
    skipped = _json_copy(inputs.skipped_accounts)
    return {
        "objects": objects,
        "facts": facts,
        "metadata": {
            "schema_version": SNAPSHOT_VERSION,
            "complete": True,
            "entity_count": len(objects),
            "fact_row_count": sum(len(rows) for rows in facts.values()),
            "accounts": accounts,
            "captured_ranges": sorted(facts),
            "digest": _digest(objects, facts, accounts, skipped),
            "no_fact_row_policy": "existing_evaluator_zero_for_fresh_account",
        },
    }


def _read(row: Any, field: str, default: Any = None) -> Any:
    return (
        row.get(field, default)
        if isinstance(row, dict)
        else getattr(row, field, default)
    )


def _restore(
    objects: list[dict[str, Any]],
    facts: dict[str, Any],
    metadata: dict[str, Any],
    skipped: list[Any],
) -> Inputs:
    entities = {}
    for raw in objects:
        item = {
            key: value
            for key, value in raw.items()
            if key not in {"before", "decimal_attributes"}
        }
        item["attributes"] = dict(item["attributes"])
        for key in raw["decimal_attributes"]:
            item["attributes"][key] = Decimal(item["attributes"][key])
        item["material_uses"] = tuple(item["material_uses"])
        item["report_ids"] = tuple(item["report_ids"])
        entity = Entity(**item)
        entities[entity.key] = entity
    return Inputs(
        accounts={
            key: AccountInfo(**value) for key, value in metadata["accounts"].items()
        },
        entities=entities,
        facts={
            window: {
                key: {
                    metric: Decimal(value) if value is not None else None
                    for metric, value in values.items()
                }
                for key, values in rows.items()
            }
            for window, rows in facts.items()
        },
        skipped_accounts=skipped,
    )


def _missing_values(definition: RuleDefinition, inputs: Inputs) -> bool:
    for entity in inputs.entities.values():
        if not in_rule_scope(entity, definition):
            continue
        for condition in definition.conditions:
            if RULE_METRICS[condition.metric].kind == "attribute":
                if entity.attributes.get(condition.metric) is None:
                    return True
            else:
                values = inputs.facts[str(condition.range)].get(entity.key)
                # 已冻结的完整窗口无事实行，保留正式判断器的零投放口径。
                if values is not None and any(
                    values.get(key) is None
                    for key in _RATIOS.get(condition.metric, (condition.metric,))
                ):
                    return True
    return False


def _replay(definition: RuleDefinition, inputs: Inputs, limit: int) -> dict[str, Any]:
    ranges = {
        str(item.range)
        for item in definition.conditions
        if RULE_METRICS[item.metric].kind == "fact"
    }
    if ranges - inputs.facts.keys():
        return {"available": False, "limitations": ["metric_range_not_captured"]}
    result = evaluate(definition, inputs)
    missing = _missing_values(definition, inputs)
    limitations = []
    if missing:
        limitations.append("missing_metric_values")
    if result.unknown_targets:
        limitations.append("unknown_judgements")
    if result.skipped_count or inputs.skipped_accounts:
        limitations.append("skipped_inputs")
    subjects = sorted(hit.subject_key for hit in result.hits)
    targets = sorted({target.key for hit in result.hits for target in hit.targets})
    truncated = (
        len(subjects) > limit
        or len(targets) > limit
        or any(hit.target_count > len(hit.targets) for hit in result.hits)
    )
    if truncated:
        limitations.append("replay_results_truncated")
    return {
        "available": True,
        "checked": result.checked,
        "hit_count": len(result.hits),
        "nonhit_count": None
        if missing or result.unknown_targets or result.skipped_count
        else result.checked - len(result.hits),
        "hit_subjects": subjects[:limit],
        "hit_targets": targets[:limit],
        "unknown_targets": list(result.unknown_targets[:limit]),
        "skipped_count": result.skipped_count,
        "limitations": limitations,
    }


def _bound_report(report: dict[str, Any], byte_limit: int) -> dict[str, Any]:
    if len(json.dumps(report, ensure_ascii=False).encode()) <= byte_limit:
        return report
    # 保留可定位原始快照的摘要，超大字段一律不送入模型上下文。
    return {
        "schema_version": report["schema_version"],
        "input_digest": report.get("input_metadata", {}).get("digest"),
        "objects": [],
        "facts": {},
        "accounts": {},
        "replay": {"available": False},
        "truncated": True,
        "complete_backtest": False,
        "limitations": ["report_size_limit"],
        "causal_outcomes_available": False,
        "claim_scope": report["claim_scope"],
    }


def rule_snapshot_report(
    row: Any,
    *,
    candidate_definition: RuleDefinition | dict[str, Any] | None = None,
    object_limit: int = 100,
    fact_cell_limit: int = 5000,
    replay_entity_limit: int = 20000,
    report_byte_limit: int = 100000,
) -> dict[str, Any]:
    """供复盘冻结输入使用的有界报告；候选仅比较同一历史样本上的命中变化。

    不重新查询当前报表，不扩展历史账户、窗口或素材身份。展示截断也必须
    标记不能声称完整回测；完整源快照仍由 evidence_id 定位。
    """
    object_limit = max(1, min(object_limit, 1000))
    fact_cell_limit = max(1, min(fact_cell_limit, 20000))
    report_byte_limit = max(1000, min(report_byte_limit, 200000))
    metadata = (_read(row, "data_freshness", {}) or {}).get("evaluation_inputs", {})
    objects = list(_read(row, "checked_objects", []) or [])
    facts = _read(row, "checked_metrics", {}) or {}
    skipped = list(_read(row, "skipped_accounts", []) or [])
    report: dict[str, Any] = {
        "schema_version": metadata.get("schema_version"),
        "objects": [],
        "facts": {},
        "accounts": {},
        "replay": {"available": False},
        "truncated": False,
        "complete_backtest": False,
        "limitations": [],
        "causal_outcomes_available": False,
        "claim_scope": "same_snapshot_hit_differences_only; no causal savings or false-positive claims",
    }
    if metadata.get("schema_version") != SNAPSHOT_VERSION:
        report["limitations"] = ["historical_inputs_unavailable"]
        return report
    try:
        valid = (
            metadata.get("complete") is True
            and metadata["entity_count"] == len(objects)
            and metadata["digest"]
            == _digest(objects, facts, metadata["accounts"], skipped)
        )
    except KeyError, TypeError, ValueError:
        valid = False
    if not valid:
        report["limitations"] = ["snapshot_incomplete"]
        return report
    # accounts 单独有界展示；未截断报告可用该字段与元数据还原并验证摘要。
    report["input_metadata"] = {
        key: value for key, value in metadata.items() if key != "accounts"
    }
    selected = objects[:object_limit]
    keys = {item["key"] for item in selected}
    report["objects"] = _json_copy(selected)
    report["entity_count"] = len(objects)
    report["accounts"] = dict(list(metadata["accounts"].items())[:object_limit])
    report["captured_ranges"] = metadata["captured_ranges"]
    cells = 0
    for window, rows in facts.items():
        report["facts"][window] = {}
        for key, values in rows.items():
            if key in keys and cells + len(values) <= fact_cell_limit:
                report["facts"][window][key] = dict(values)
                cells += len(values)
            else:
                report["truncated"] = True
    report["truncated"] |= (
        len(objects) > object_limit or len(metadata["accounts"]) > object_limit
    )
    if report["truncated"]:
        report["limitations"].append("report_truncated")
    # 单对象可能含成千上万个素材使用位置；对象数限制不能代替字节限制。
    if len(json.dumps(report, ensure_ascii=False).encode()) > report_byte_limit:
        return _bound_report(report, report_byte_limit)
    if len(objects) > min(replay_entity_limit, 20000):
        report["limitations"].append("replay_input_limit")
        return report
    try:
        inputs = _restore(objects, facts, metadata, skipped)
        definition = RuleDefinition.model_validate(_read(row, "definition"))
        current = _replay(definition, inputs, object_limit)
        report["replay"] = {"available": current["available"], "current": current}
        report["limitations"].extend(current["limitations"])
        if candidate_definition is not None:
            candidate = RuleDefinition.model_validate(candidate_definition)
            # 账户/调度决定原始加载范围；素材门控也依赖原条件，不能假装新查询已执行。
            same_scope = (
                candidate.accounts == definition.accounts
                and candidate.schedule == definition.schedule
                and candidate.apply_to == definition.apply_to
                and candidate.action.level == definition.action.level
                and candidate.aggregation == definition.aggregation
            )
            material_safe = definition.apply_to.level != "material" or {
                (c.metric, c.range) for c in candidate.conditions
            } == {(c.metric, c.range) for c in definition.conditions}
            candidate_result = (
                _replay(candidate, inputs, object_limit)
                if same_scope and material_safe
                else {
                    "available": False,
                    "limitations": ["candidate_input_scope_not_captured"],
                }
            )
            report["replay"]["candidate"] = candidate_result
            report["limitations"].extend(candidate_result["limitations"])
            if (
                current["available"]
                and candidate_result["available"]
                and not current["limitations"]
                and not candidate_result["limitations"]
            ):
                before, after = (
                    set(current["hit_subjects"]),
                    set(candidate_result["hit_subjects"]),
                )
                report["replay"]["comparison"] = {
                    "added_hit_subjects": sorted(after - before),
                    "removed_hit_subjects": sorted(before - after),
                    "unchanged_hit_count": len(before & after),
                }
    except KeyError, TypeError, ValueError, ArithmeticError:
        report["replay"] = {"available": False}
        report["limitations"].append("invalid_snapshot_or_definition")
    report["limitations"] = sorted(set(report["limitations"]))
    report["complete_backtest"] = (
        report["replay"]["available"] and not report["limitations"]
    )
    return _bound_report(report, report_byte_limit)
