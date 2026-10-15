"""推广系列批量改名：按规则逐条算出新名称、校验并冻结为管理命令。

名称在预览阶段一次算定并写入 final_value，提交和重试只消费冻结值。
改名会改变剧名解析结果，从而改变报表按剧归类，因此每条都附带解析后的剧名供界面提示。
"""

import re
from collections import Counter
from copy import deepcopy
from typing import Any

from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.integrations.tiktok.contracts.ads import EntityRef
from app.integrations.tiktok.contracts.management import ManagementCommand
from app.modules.ad_management.expansion import (
    ExpandedItem,
    Expansion,
    _grouping_revision,
    _management_allowed,
    _object_for_ref,
)
from app.modules.ad_management.schemas import MutationSpec, RenameRule
from app.modules.ads.models import AdObject
from app.modules.ads.naming import parse_campaign_name

MAX_NAME_LENGTH = 512
# 平台不接受表情符号；按常见表情所在的 Unicode 区段与表情变体选择符判断。
_EMOJI = re.compile("[\U0001f000-\U0001faff☀-➿️]")


def _clean(name: str) -> str:
    # 飞书/Excel 复制常带全角空格和换行，统一去掉首尾空白后再比较与写入。
    return name.strip()


def compute_name(current: str, rule: RenameRule, remote_id: str) -> str | None:
    if rule.kind == "explicit":
        new = rule.names.get(remote_id)
        return None if new is None else _clean(new)
    if rule.kind == "replace":
        assert rule.find is not None
        return _clean(current.replace(rule.find, rule.replace_with))
    # 前后缀已存在时不重复添加，同一规则重复执行结果不变。
    name = current
    if rule.prefix and not name.startswith(rule.prefix):
        name = rule.prefix + name
    if rule.suffix and not name.endswith(rule.suffix):
        name = name + rule.suffix
    return _clean(name)


def name_problem(name: str) -> str | None:
    if not name.strip():
        return "name_empty"
    if len(name) > MAX_NAME_LENGTH:
        return "name_too_long"
    if _EMOJI.search(name):
        return "name_has_emoji"
    return None


def _drama_hint(old: str, new: str | None) -> dict[str, Any]:
    """新名称解析出的剧名；无法解析时视为剧归类发生变化，需要界面提醒。"""
    if new is None:
        return {}
    parsed = parse_campaign_name(new)
    if parsed.status != "VALID":
        return {"drama_changed": True}
    before = parse_campaign_name(old)
    return {
        "drama_name": parsed.drama_name,
        "provider_label": parsed.provider_label,
        "drama_changed": (parsed.provider_label, parsed.drama_name)
        != (before.provider_label, before.drama_name),
    }


def expand_rename(
    session: Session,
    context: TenantContext,
    *,
    bc_id: str,
    refs: tuple[EntityRef, ...],
    mutation: MutationSpec,
    route: Any,
    require_capabilities: bool,
) -> Expansion:
    rule = mutation.rename
    assert rule is not None
    # 每个位置先放占位（None），全部校验完成后按原顺序输出，保证预览项顺序与选择一致。
    slots: list[ExpandedItem | None] = []
    # 仍可能改名的候选：(位置, 目录行, 新名称)。
    candidates: list[tuple[int, AdObject, str]] = []

    def unsupported(ref: EntityRef, reason: str, **extra: Any) -> ExpandedItem:
        return ExpandedItem(
            ref=ref,
            material_use=None,
            original_value=extra.pop("original_value", None),
            final_value=extra.pop("final_value", None),
            reason=reason,
            execution_result="UNSUPPORTED",
            command=None,
            parent_ref=None,
            grouping_revision=extra.pop("grouping_revision", 0),
            capability=extra.pop("capability", {}),
            **extra,
        )

    rows: dict[int, AdObject] = {}
    capabilities: dict[int, dict[str, Any]] = {}
    for position, ref in enumerate(refs):
        if ref.kind != "campaign":
            slots.append(unsupported(ref, "rename_campaign_only"))
            continue
        row = _object_for_ref(
            session, context=context, bc_id=bc_id, ref=ref, route=route
        )
        if row is None:
            slots.append(unsupported(ref, "ad_not_found"))
            continue
        rows[position] = row
        capabilities[position] = {
            "operation": "rename",
            "entity_kind": row.kind,
            "ad_type": row.ad_type,
            "published_version": row.published_version,
            "observed_at": row.observed_at.isoformat(),
            "configuration": deepcopy(row.configuration or {}),
            "grouping_revision": _grouping_revision(session, row.ref),
            "source_connection_id": str(row.source_connection_id)
            if row.source_connection_id
            else None,
            "source_channel": row.source_channel,
            **({"route": route.model_dump(mode="json")} if route is not None else {}),
        }
        current = _clean(row.name)
        new = compute_name(current, rule, row.remote_id)
        common = {
            "name": row.name,
            "original_value": current,
            "grouping_revision": capabilities[position]["grouping_revision"],
            "capability": capabilities[position],
        }
        if new is None:
            slots.append(unsupported(ref, "rename_missing", **common))
            continue
        problem = name_problem(new)
        if problem is not None:
            slots.append(
                unsupported(
                    ref,
                    problem,
                    final_value=new,
                    **common,
                    **_drama_hint(row.name, new),
                )
            )
            continue
        slots.append(None)
        candidates.append((position, row, new))

    def reject(position: int, reason: str, new: str) -> None:
        row = rows[position]
        slots[position] = unsupported(
            row.ref,
            reason,
            name=row.name,
            original_value=_clean(row.name),
            final_value=new,
            grouping_revision=capabilities[position]["grouping_revision"],
            capability=capabilities[position],
            **_drama_hint(row.name, new),
        )

    # 同一账户内平台要求系列名唯一：本批算出同名的全部拒绝，不替用户挑选保留哪一条。
    batch_counts = Counter((row.advertiser_id, new) for _, row, new in candidates)
    remaining: list[tuple[int, AdObject, str]] = []
    for position, row, new in candidates:
        if batch_counts[(row.advertiser_id, new)] > 1:
            reject(position, "name_duplicate_in_batch", new)
        else:
            remaining.append((position, row, new))

    # 与同账户中不参与本次改名的系列（含本批校验失败、保留原名的系列）比对重名。
    renaming = {(row.advertiser_id, row.remote_id) for _, row, _ in remaining}
    taken: set[tuple[str, str]] = set()
    if remaining:
        account_rows = session.exec(
            select(AdObject.advertiser_id, AdObject.remote_id, AdObject.name).where(
                AdObject.tenant_id == context.tenant_id,
                col(AdObject.advertiser_id).in_(
                    {row.advertiser_id for _, row, _ in remaining}
                ),
                AdObject.kind == "campaign",
            )
        ).all()
        taken = {
            (advertiser_id, _clean(name))
            for advertiser_id, remote_id, name in account_rows
            if (advertiser_id, remote_id) not in renaming
        }

    commands: list[ManagementCommand] = []
    for position, row, new in remaining:
        if (row.advertiser_id, new) in taken:
            reject(position, "name_duplicate_in_account", new)
            continue
        if require_capabilities and not _management_allowed(
            session,
            context=context,
            bc_id=bc_id,
            advertiser_id=row.advertiser_id,
            route=route,
            operation="rename",
            entity_kind=row.kind,
        ):
            reject(position, "management_capability_unverified", new)
            continue
        current = _clean(row.name)
        capability = capabilities[position]
        hint = _drama_hint(row.name, new)
        if new == current:
            slots[position] = ExpandedItem(
                ref=row.ref,
                material_use=None,
                original_value=current,
                final_value=new,
                reason="no_change",
                execution_result="NO_CHANGE",
                command=None,
                parent_ref=row.parent_ref,
                grouping_revision=capability["grouping_revision"],
                capability=capability,
                name=row.name,
                **hint,
            )
            continue
        command = ManagementCommand(
            row.ref,
            "name",
            {"ad_type": row.ad_type, **deepcopy(row.configuration or {})},
            {"campaign_name": new},
        )
        commands.append(command)
        slots[position] = ExpandedItem(
            ref=row.ref,
            material_use=None,
            original_value=current,
            final_value=new,
            reason=None,
            execution_result="PENDING",
            command=command,
            parent_ref=row.parent_ref,
            grouping_revision=capability["grouping_revision"],
            capability=capability,
            name=row.name,
            **hint,
        )

    items = tuple(item for item in slots if item is not None)
    return Expansion(refs, (), items, tuple(commands))


__all__ = ["compute_name", "expand_rename", "name_problem"]
