"""只从实际响应生成回读事实；完整单页不代表找到了对象或全扫描完成。"""

import re
from typing import Literal, cast

from pydantic import ValidationError

from app.integrations.tiktok.contracts.builds import (
    AdGroupCreate,
    AdGroupObservedFacts,
    AdGroupStatus,
    BuildPage,
    BuildReadQuery,
    BuildRecord,
)
from app.integrations.tiktok.contracts.common import (
    CallEvidence,
    McpBusinessResponse,
    RemoteCallError,
)
from app.modules.builds.request_compiler import decode_observed_intent, encode_intent

PAGE_SIZE = 100
ID_KEYS = {
    "CAMPAIGN": "campaign_id",
    "ADGROUP": "adgroup_id",
    "AD": "smart_plus_ad_id",
    "CTA": "creative_portfolio_id",
}


def readback_error(evidence: CallEvidence) -> RemoteCallError:
    return RemoteCallError(
        "build_readback_unverified", effect="UNKNOWN", evidence=evidence
    )


def page_rows(
    *, data: object, page: int, evidence: CallEvidence
) -> tuple[list[dict[str, object]], int, int]:
    if not isinstance(data, dict):
        raise readback_error(evidence)
    rows, info = data.get("list"), data.get("page_info")
    if (
        not isinstance(rows, list)
        or not all(isinstance(row, dict) for row in rows)
        or not isinstance(info, dict)
    ):
        raise readback_error(evidence)
    numbers = [
        info.get(key) for key in ("page", "page_size", "total_page", "total_number")
    ]
    if any(type(value) is not int for value in numbers):
        raise readback_error(evidence)
    actual, size, pages, total = cast(list[int], numbers)
    if (
        actual != page
        or size != PAGE_SIZE
        or not 0 <= pages <= 1000
        or total < 0
        or (
            pages != (total + PAGE_SIZE - 1) // PAGE_SIZE
            and not (total == 0 and pages == 1)
        )
        or not 1 <= page <= max(1, pages)
        or len(rows) != min(PAGE_SIZE, max(0, total - (page - 1) * PAGE_SIZE))
    ):
        raise readback_error(evidence)
    return rows, pages, total


def _project(shape: object, actual: object, path: str, missing: list[str]) -> object:
    # shape 只提供必须观察的字段路径；所有返回值均来自 actual，绝不复制请求值。
    if isinstance(shape, dict):
        if not isinstance(actual, dict):
            missing.append(path)
            return None
        result = {}
        for key, child in shape.items():
            child_path = f"{path}.{key}" if path else key
            if key not in actual or actual[key] is None:
                missing.append(child_path)
            else:
                result[key] = _project(child, actual[key], child_path, missing)
        return result
    if isinstance(shape, list):
        if not isinstance(actual, list):
            missing.append(path)
            return None
        return (
            [
                _project(shape[0], item, f"{path}.{index}", missing)
                for index, item in enumerate(actual)
            ]
            if shape
            else actual
        )
    return actual


def _safe_status(value: object) -> str | None:
    return (
        value
        if type(value) is str
        and 0 < len(value) <= 128
        and all(char.isalnum() or char == "_" for char in value)
        else None
    )


def safe_remote_id(value: object) -> str | None:
    # 与创建回执使用同一远端对象 ID 语法；名称/文案的共用 Id 类型不收紧。
    return (
        value
        if type(value) is str and re.fullmatch(r"[A-Za-z0-9_.:-]{1,255}", value)
        else None
    )


def parse_page(*, query: BuildReadQuery, response: McpBusinessResponse) -> BuildPage:
    kind = query.intent.kind
    if kind == "CTA":
        if (
            query.remote_id is None
            or query.page != 1
            or not isinstance(response.data, dict)
        ):
            raise readback_error(response.evidence)
        rows, pages, total = [response.data], 1, 1
    else:
        rows, pages, total = page_rows(
            data=response.data, page=query.page, evidence=response.evidence
        )
    records = []
    seen = set()
    shape = encode_intent(query.intent)
    for row in rows:
        remote_id = safe_remote_id(row.get(ID_KEYS[kind]))
        if remote_id is None or remote_id in seen:
            raise readback_error(response.evidence)
        seen.add(remote_id)
        status = row.get("operation_status")
        if type(status) is not str or not status.strip():
            status = None
        missing: list[str] = []
        projected = _project(shape, row, "", missing)
        observed_adgroup = None
        # Smart+组接口缺status时仅保留其余实际观察到的完整typed字段，不补请求默认值。
        if (
            kind == "ADGROUP"
            and isinstance(projected, dict)
            and not any(field != "operation_status" for field in missing)
        ):
            core = dict(projected)
            core.pop("operation_status", None)
            targeting = core.pop("targeting_spec", None)
            if isinstance(targeting, dict) and "location_ids" in targeting:
                core["location_ids"] = targeting["location_ids"]
                core["name"] = core.pop("adgroup_name", None)
                try:
                    observed_adgroup = AdGroupObservedFacts.model_validate(core)
                except ValidationError:
                    pass
        intent = None
        if not missing and isinstance(projected, dict):
            try:
                intent = decode_observed_intent(kind, projected)
            except ValidationError as error:
                missing.extend(
                    ".".join(str(part) for part in item["loc"]) or "intent"
                    for item in error.errors()
                )
        records.append(
            BuildRecord(
                remote_id=remote_id,
                intent=intent,
                operation_status=status,
                missing_fields=tuple(sorted(set(missing))),
                observed_adgroup=observed_adgroup,
                review_status=_safe_status(row.get("secondary_status")),
            )
        )
    return BuildPage(
        rows=tuple(records),
        page=query.page,
        total_pages=pages,
        total_number=total,
        complete=True,
        evidence=response.evidence,
    )


def _equal(left: object, right: object) -> bool:
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _equal(value, right[key]) for key, value in left.items()
        )
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        remaining = list(right)
        for item in left:
            matched = next(
                (index for index, value in enumerate(remaining) if _equal(item, value)),
                None,
            )
            if matched is None:
                return False
            remaining.pop(matched)
        return not remaining
    return type(left) is type(right) and left == right


def compare_record(
    *, query: BuildReadQuery, record: BuildRecord
) -> Literal["MATCH", "MISMATCH", "INCOMPLETE"]:
    if query.remote_id is not None and record.remote_id != query.remote_id:
        return "MISMATCH"
    # 明确停用优先于缺字段：不能把已知状态不一致隐藏成仅证据不足。
    if (
        query.intent.kind != "CTA"
        and record.operation_status is not None
        and record.operation_status != "ENABLE"
    ):
        return "MISMATCH"
    if record.intent is None or record.missing_fields:
        return "INCOMPLETE"
    return (
        "MATCH"
        if _equal(query.intent.model_dump(), record.intent.model_dump())
        else "MISMATCH"
    )


def parse_status(
    *, advertiser_id: str, adgroup_id: str, response: McpBusinessResponse
) -> AdGroupStatus:
    rows, _, total = page_rows(data=response.data, page=1, evidence=response.evidence)
    if total != 1 or len(rows) != 1:
        raise readback_error(response.evidence)
    row = rows[0]
    if (
        safe_remote_id(row.get("advertiser_id")) is None
        or safe_remote_id(row.get("adgroup_id")) is None
        or row.get("advertiser_id") != advertiser_id
        or row.get("adgroup_id") != adgroup_id
    ):
        raise readback_error(response.evidence)
    try:
        return AdGroupStatus.model_validate(
            {
                "advertiser_id": row["advertiser_id"],
                "adgroup_id": row["adgroup_id"],
                "operation_status": row.get("operation_status"),
                "evidence": response.evidence,
                "review_status": _safe_status(row.get("secondary_status")),
            }
        )
    except ValidationError:
        raise readback_error(response.evidence) from None


def with_adgroup_status(*, record: BuildRecord, status: AdGroupStatus) -> BuildRecord:
    """只将同账户/同对象的实际状态与既有强类型观察合并；父级字段从未重填。"""
    observed = record.observed_adgroup
    if (
        observed is None
        or status.advertiser_id != observed.advertiser_id
        or status.adgroup_id != record.remote_id
        or any(field != "operation_status" for field in record.missing_fields)
    ):
        raise readback_error(status.evidence)
    intent = None
    missing: tuple[str, ...] = ("operation_status",)
    if status.operation_status == "ENABLE":
        intent = AdGroupCreate(**observed.model_dump(), operation_status="ENABLE")
        missing = ()
    return record.model_copy(
        update={
            "intent": intent,
            "operation_status": status.operation_status,
            "missing_fields": missing,
            "review_status": status.review_status
            if status.review_status is not None
            else record.review_status,
        }
    )
