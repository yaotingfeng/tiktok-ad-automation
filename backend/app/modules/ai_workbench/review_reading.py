"""按任务冻结索引分页读取；目录不是已读证据，不重新查询实时业务事实。"""

import json
from copy import deepcopy
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import HTTPException

from app.core.errors import DomainError
from app.modules.ai_workbench.input_budget import MAX_INPUT_BYTES
from app.modules.ai_workbench.reviews import _digest

PAGE_SIZE = 10
CATEGORIES = (
    "plans",
    "build_occurrences",
    "rule_evidence",
    "attribution",
    "material_orders",
    "history",
    "business",
    "operation_logs",
    "knowledge_refs",
)


def split_evidence(frozen: dict) -> tuple[dict, dict]:
    """复用已鉴权且有截止时间的冻结结果；正文独立保存，不随提示词截断丢失。"""
    store = {}
    index = []

    def archive(category, payload, identity=None):
        digest = _digest(payload)
        source_id = identity or str(uuid5(NAMESPACE_URL, f"tkada:{category}:{digest}"))
        store[source_id] = {"category": category, "payload": payload, "digest": digest}
        index.append({"id": source_id, "category": category, "sha256": digest})
        return source_id

    summaries = []
    for report in frozen["data_pack"]["reporting"]:
        source_id = archive("reporting", report, report.get("id"))
        index[-1].update(
            {
                key: report.get(key)
                for key in (
                    "advertiser_id",
                    "dimension",
                    "maturity_horizon",
                    "status",
                    "window",
                    "rows_truncated",
                    "total",
                )
            }
        )
        if report["dimension"] == "account":
            summaries.append(
                {key: value for key, value in report.items() if key != "rows"}
                | {"id": source_id}
            )
    for category in CATEGORIES:
        archive(category, frozen.get(category, {}))
        limit_key = {
            "plans": "plans",
            "build_occurrences": "builds",
            "rule_evidence": "rules",
        }.get(category)
        if limit_key:
            index[-1]["truncated"] = frozen.get("execution_truncated", {}).get(
                limit_key, False
            )
    for category in ("business", "operation_events"):
        archive("data_pack_" + category, frozen["data_pack"].get(category, []))
    pack = {
        key: value
        for key, value in frozen["data_pack"].items()
        if key not in {"reporting", "business", "operation_events"}
    }
    initial = {
        key: deepcopy(frozen[key])
        for key in (
            "optimizer",
            "strategy",
            "rules",
            "captured_at",
            "capabilities",
            "knowledge",
            "knowledge_selection",
        )
    }
    initial.update(
        reading_contract="on-demand-v1",
        data_pack={**pack, "reporting": summaries},
        evidence_index=index,
        read_evidence_ids=[row["id"] for row in summaries],
    )
    # 固定范围和索引必须完整；过大时明确拒绝，不能悄悄丢失证据。
    from app.modules.ai_workbench.methods import METHOD_ARCHIVE_BYTES

    if (
        len(json.dumps(initial, ensure_ascii=False).encode())
        > MAX_INPUT_BYTES - METHOD_ARCHIVE_BYTES - 200000
    ):
        raise DomainError(
            "ai_review_input_too_large", "复盘固定摘要与索引超过容量，请缩小范围"
        )
    return initial, store


def query_for_task(*, task, category: str, offset: int = 0) -> dict:
    rows = [
        row for row in task.evidence["evidence_index"] if row["category"] == category
    ]
    return {
        "items": rows[offset : offset + PAGE_SIZE],
        "next_offset": offset + PAGE_SIZE if len(rows) > offset + PAGE_SIZE else None,
        "cutoff": task.evidence["captured_at"],
    }


def read_for_task(
    session, *, task, evidence_id: UUID, offset: int = 0, limit: int = 1
) -> dict:
    if offset < 0 or not 1 <= limit <= PAGE_SIZE:
        raise HTTPException(422, "invalid_evidence_page")
    key = str(evidence_id)
    entry = next(
        (row for row in task.evidence["evidence_index"] if row["id"] == key), None
    )
    source = task.evidence_store.get(key)
    if entry is None or source is None:
        raise HTTPException(404, "evidence_not_available_in_task_scope")
    if (
        source["category"] != entry["category"]
        or source["digest"] != entry["sha256"]
        or _digest(source["payload"]) != source["digest"]
    ):
        raise DomainError("ai_evidence_changed", "冻结证据摘要不一致")
    payload = source["payload"]
    if isinstance(payload, list):
        rows, metadata = payload, {"truncated": entry.get("truncated", False)}
    elif isinstance(payload, dict) and isinstance(payload.get("rows"), list):
        rows = payload["rows"]
        metadata = {k: v for k, v in payload.items() if k != "rows"}
        if source["category"] == "attribution":
            # 一条归因可含多个窗口/大量报表行，拆成精确身份+单窗口单行供分页。
            rows = [
                {
                    **mapping,
                    "reports": [{**report, "rows": [row] if row is not None else []}],
                }
                for mapping in rows
                for report in mapping.get("reports", [])
                for row in (report.get("rows") or [None])
            ]
    elif isinstance(payload, dict):
        # 历史/发布索引按单条记录分页，不能一次返回整个历史列表。
        groups = {k: v for k, v in payload.items() if isinstance(v, list)}
        rows = [
            {"section": name, "value": row}
            for name, values in groups.items()
            for row in values
        ]
        metadata = {k: v for k, v in payload.items() if k not in groups}
        if not groups:
            rows, metadata = [payload], {}
    else:
        rows, metadata = [payload], {}
    if offset and offset >= len(rows):
        raise HTTPException(422, "invalid_evidence_page")
    page_id = str(uuid5(task.id, f"{key}:{source['digest']}:{offset}:{limit}"))
    result = {
        "id": page_id,
        "source_id": key,
        "category": source["category"],
        "cutoff": task.evidence["captured_at"],
        "metadata": metadata,
        "rows": rows[offset : offset + limit],
        "next_offset": offset + limit if len(rows) > offset + limit else None,
        "frozen_rows": len(rows),
    }
    # 只有成功返回的页 ID 可引用；索引/未读页不获得权限，重复读取幂等。
    if len(json.dumps(result, ensure_ascii=False).encode()) > 64000:
        raise DomainError(
            "ai_evidence_page_too_large", "证据页超过容量，请缩小复盘范围"
        )
    read_ids = set(task.evidence.get("read_evidence_ids", []))
    if len(read_ids) >= 1000 and page_id not in read_ids:
        raise DomainError("ai_evidence_read_budget", "本轮证据读取页数已达上限")
    delivered_ids = {page_id}
    if source["category"] == "rule_evidence":
        delivered_ids.update(str(row["id"]) for row in result["rows"])
    updated = {
        **task.evidence,
        "read_evidence_ids": sorted(read_ids | delivered_ids),
        "read_receipts": {
            **task.evidence.get("read_receipts", {}),
            page_id: {
                "source_id": key,
                "sha256": source["digest"],
                "offset": offset,
                "limit": limit,
            },
        },
    }
    if len(json.dumps(updated, ensure_ascii=False).encode()) > MAX_INPUT_BYTES:
        raise DomainError("ai_evidence_read_budget", "本轮证据读取记录超过容量")
    task.evidence = updated
    session.add(task)
    session.flush()
    return result
