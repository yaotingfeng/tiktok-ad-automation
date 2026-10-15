"""推广系列批量改名：按规则逐条算出新名称、校验并冻结为管理命令。

名称在预览阶段一次算定并写入 final_value，提交和重试只消费冻结值。
改名会改变剧名解析结果，从而改变报表按剧归类，因此每条都附带解析后的剧名供界面提示。
"""

import re
from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.integrations.tiktok.contracts.ads import EntityRef
from app.integrations.tiktok.contracts.management import ManagementCommand
from app.modules.accounts.access import usable_grants
from app.modules.accounts.models import BCAccountAccess
from app.modules.ad_management.expansion import (
    ExpandedItem,
    Expansion,
    _grouping_revision,
    _management_allowed,
    _object_for_ref,
)
from app.modules.ad_management.schemas import (
    MutationSpec,
    RenameRowProblem,
    RenameRule,
    RenameSelectionPublic,
    RenameSelectionRequest,
)
from app.modules.ads.models import AdObject
from app.modules.ads.naming import parse_campaign_name
from app.modules.reporting.selection import (
    create_target_selection,
    get_frozen_selection,
)
from app.modules.tenants.permissions import require_tenant

MAX_NAME_LENGTH = 512
# 平台不接受表情符号，按以下区段判断：U+1F000-1FAFF 主要表情与符号平面，
# U+2600-27BF 杂项符号与装饰符号，U+2B00-2BFF 箭头/星形等常被渲染成表情的符号，
# U+203C、U+2049 双叹号与叹问号，U+FE0F 表情变体选择符，U+200D 表情拼接用的零宽连接符。
_EMOJI = re.compile(
    "[\U0001f000-\U0001faff\u2600-\u27bf\u2b00-\u2bff\u203c\u2049\ufe0f\u200d]"
)


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
                    # 超长名称存不进 512 列，只记原因、不冻结新值，避免整个预览失败。
                    final_value=None if problem == "name_too_long" else new,
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

    # 先排除不会真正改名的行：能力未核验或新旧同名的行保留原名，
    # 它们的原名仍占用账户内的名称，必须参与下面的重名判断。
    renaming: list[tuple[int, AdObject, str]] = []
    for position, row, new in candidates:
        if new == _clean(row.name):
            capability = capabilities[position]
            slots[position] = ExpandedItem(
                ref=row.ref,
                material_use=None,
                original_value=new,
                final_value=new,
                reason="no_change",
                execution_result="NO_CHANGE",
                command=None,
                parent_ref=row.parent_ref,
                grouping_revision=capability["grouping_revision"],
                capability=capability,
                name=row.name,
                **_drama_hint(row.name, new),
            )
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
        renaming.append((position, row, new))

    # 同一账户内平台要求系列名唯一：本批算出同名的全部拒绝，不替用户挑选保留哪一条。
    batch_counts = Counter((row.advertiser_id, new) for _, row, new in renaming)
    remaining: list[tuple[int, AdObject, str]] = []
    for position, row, new in renaming:
        if batch_counts[(row.advertiser_id, new)] > 1:
            reject(position, "name_duplicate_in_batch", new)
        else:
            remaining.append((position, row, new))

    # 新名称不得等于同账户任何其他系列的当前名称，包括本批中也要改名的系列：
    # 执行是逐条串行提交的，互换（A→B、B→A）或链式（A→B、B→C）改名必然有一步
    # 撞上尚未改走的旧名，因此预览直接拒绝，互换需分两批完成。
    # 范围故意取目录中该账户的全部推广系列（含已删除）：尚未确认 TikTok
    # 是否允许复用已删除系列的名称，按保守口径处理。
    holders: dict[tuple[str, str], set[str]] = {}
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
        for advertiser_id, remote_id, name in account_rows:
            holders.setdefault((advertiser_id, _clean(name)), set()).add(remote_id)

    commands: list[ManagementCommand] = []
    for position, row, new in remaining:
        if holders.get((row.advertiser_id, new), set()) - {row.remote_id}:
            reject(position, "name_duplicate_in_account", new)
            continue
        current = _clean(row.name)
        capability = capabilities[position]
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
            **_drama_hint(row.name, new),
        )

    items = tuple(item for item in slots if item is not None)
    return Expansion(refs, (), items, tuple(commands))


def resolve_rename_rows(
    session: Session, context: TenantContext, request: RenameSelectionRequest
) -> RenameSelectionPublic:
    """把表格粘贴的推广系列 ID 解析成本 BC 内的冻结选择，并列出表格层面的问题行。

    新名称不在此校验：名称合法性与重名统一由改名预览判定。
    """
    require_tenant(
        session,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        action="ads_manage",
    )
    problems: list[RenameRowProblem] = []
    ids: list[str] = []
    new_names: dict[str, str] = {}
    for row in request.rows:
        campaign_id = row.campaign_id.strip()
        # Excel 会把长数字显示成 1.7E+18 之类的科学计数法，精度已丢失，只能拒绝。
        # 只认 ASCII 数字：中文输入法粘贴出的全角数字也算格式错误，避免误报为找不到。
        if not (campaign_id.isascii() and campaign_id.isdigit()):
            problems.append(
                RenameRowProblem(
                    campaign_id=campaign_id, new_name=row.new_name, reason="id_invalid"
                )
            )
        elif campaign_id in ids:
            # 同一 ID 只保留第一次出现的行，后续行无法确定以哪个新名称为准。
            problems.append(
                RenameRowProblem(
                    campaign_id=campaign_id,
                    new_name=row.new_name,
                    reason="duplicate_campaign_id",
                )
            )
        else:
            ids.append(campaign_id)
            new_names[campaign_id] = row.new_name
    # 只在本 BC 当前可读的账户内查找；与读取冻结选择时的授权校验口径一致。
    advertisers = usable_grants(
        tenant_id=context.tenant_id, bc_id=request.bc_id, action="read"
    ).with_only_columns(col(BCAccountAccess.advertiser_id))
    found = session.exec(
        select(AdObject)
        .where(
            AdObject.tenant_id == context.tenant_id,
            AdObject.kind == "campaign",
            col(AdObject.remote_id).in_(ids),
            col(AdObject.advertiser_id).in_(advertisers),
        )
        .order_by(col(AdObject.advertiser_id))
    ).all()
    by_id = {obj.remote_id: obj for obj in found}
    refs: list[dict[str, str]] = []
    for campaign_id in ids:
        obj = by_id.get(campaign_id)
        if obj is None:
            # 不区分"不在本 BC"与"不存在"，避免泄露其他 BC 的推广系列信息。
            problems.append(
                RenameRowProblem(
                    campaign_id=campaign_id,
                    new_name=new_names[campaign_id],
                    reason="not_found",
                )
            )
            continue
        refs.append(
            {
                "tenant_id": str(context.tenant_id),
                "advertiser_id": obj.advertiser_id,
                "kind": "campaign",
                "remote_id": campaign_id,
            }
        )
    if not refs:
        return RenameSelectionPublic(selection=None, problems=problems)
    record = create_target_selection(
        session,
        context=context,
        bc_id=request.bc_id,
        refs=refs,
        uses=[],
        filters={"source": "rename_paste"},
        now=datetime.now(UTC),
    )
    selection = get_frozen_selection(
        session, context=context, bc_id=request.bc_id, selection_id=record.id
    )
    return RenameSelectionPublic(selection=selection, problems=problems)


__all__ = ["compute_name", "expand_rename", "name_problem", "resolve_rename_rows"]
