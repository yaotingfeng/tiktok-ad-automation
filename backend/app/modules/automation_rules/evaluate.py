"""纯函数判断引擎:按统计口径分组、计算指标、判断条件、展开执行对象。"""

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.modules.automation_rules.catalog import RULE_METRICS
from app.modules.automation_rules.inputs import Entity, Inputs
from app.modules.automation_rules.schemas import Condition, RuleDefinition
from app.modules.reporting.metric_catalog import DERIVED_RATIOS, STORED_KEYS

_DEPTH = {"campaign": 0, "adgroup": 1, "ad": 2, "material": 3}
_RATIOS = {name: (num, den, scale) for name, num, den, scale in DERIVED_RATIOS}
_STATUS = {"ACTIVE": "ENABLE", "PAUSED": "DISABLE"}


# 单条命中保存的执行对象上限，保护运行记录体积；正常规模下执行全部命中对象。
MAX_STORED_TARGETS = 5000


@dataclass(frozen=True)
class Target:
    key: str
    kind: str
    advertiser_id: str
    bc_id: str
    remote_id: str
    name: str
    material_uses: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class Hit:
    subject_key: str
    subject_label: str
    drama: str | None
    advertiser_id: str | None
    bc_id: str | None
    currency: str
    metrics: dict[str, str]
    # 最多保留 MAX_STORED_TARGETS 个执行对象；target_count 是真实总数。
    targets: tuple[Target, ...]
    target_count: int


@dataclass(frozen=True)
class Evaluation:
    checked: int
    hits: tuple[Hit, ...]
    skipped_count: int
    unknown_targets: tuple[str, ...] = ()


def _in_scope(entity: Entity, definition: RuleDefinition, ignore_status: bool) -> bool:
    apply = definition.apply_to
    if entity.kind != apply.level:
        return False
    if apply.status == "SELECTED":
        if f"{entity.advertiser_id}:{entity.remote_id}" not in apply.object_ids:
            return False
    elif (
        not ignore_status
        and entity.status != _STATUS[apply.status]
        and not (
            entity.kind == "material"
            and entity.judgement_issue
            and entity.status is None
        )
    ):
        return False
    return True


def _sum(
    members: list[Entity],
    facts: dict[str, dict[str, dict[str, Decimal | None]]],
    range_type: str,
    *,
    strict_facts: bool = False,
) -> dict[str, Decimal | None]:
    """没有事实行按 0 计（账户已通过新鲜度检查，说明确实没有投放）；
    有事实行但某指标全部为空时保持为空，与报表的 MISSING 口径一致，条件不满足。"""
    totals: dict[str, Decimal | None] = {key: Decimal(0) for key in STORED_KEYS}
    seen: dict[str, bool] = dict.fromkeys(STORED_KEYS, False)
    missing: dict[str, bool] = dict.fromkeys(STORED_KEYS, False)
    for member in members:
        row = facts.get(range_type, {}).get(member.key)
        if row is None:
            continue
        for key in STORED_KEYS:
            value = row.get(key)
            if value is None:
                missing[key] = True
            else:
                seen[key] = True
                totals[key] = (totals[key] or Decimal(0)) + value
    for key in STORED_KEYS:
        if missing[key] and (strict_facts or not seen[key]):
            totals[key] = None
    return totals


def _metric(
    condition: Condition,
    members: list[Entity],
    inputs: Inputs,
    *,
    strict_facts: bool = False,
) -> Decimal | str | None:
    meta = RULE_METRICS[condition.metric]
    if meta.kind == "attribute":
        return members[0].attributes.get(condition.metric)
    totals = _sum(
        members, inputs.facts, str(condition.range), strict_facts=strict_facts
    )
    if condition.metric in _RATIOS:
        num, den, scale = _RATIOS[condition.metric]
        a, b = totals.get(num), totals.get(den)
        # 分母为 0 或缺失时没有比率,条件视为不满足。
        if a is None or not b:
            return None
        # 比率类（点击率）在编辑器和报表里都按百分数填写、展示，判断与命中指标同样用百分数。
        return a / b * scale * (100 if meta.unit == "ratio" else 1)
    return totals.get(condition.metric)


def _matches(condition: Condition, value: Decimal | str | None) -> bool:
    if value is None:
        return False
    op, values = condition.operator, condition.values
    if isinstance(value, str):
        target = values[0]
        return bool(
            {
                "CONTAINS": target in value,
                "NOT_CONTAINS": target not in value,
                "STARTS_WITH": value.startswith(target),
                "ENDS_WITH": value.endswith(target),
                "EQ": value == target,
            }[op]
        )
    numbers = [Decimal(v) for v in values]
    if op == "GT":
        return bool(value > numbers[0])
    if op == "LT":
        return bool(value < numbers[0])
    if op == "EQ":
        return bool(value == numbers[0])
    return bool(numbers[0] < value < numbers[1])


def _metric_key(condition: Condition) -> str:
    return (
        f"{condition.metric}:{condition.range}" if condition.range else condition.metric
    )


def _format(value: Decimal | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        # 比率保留 4 位小数；去掉多余的 0，避免出现科学计数法。
        if value != value.to_integral():
            value = value.quantize(Decimal("0.0001"))
        return format(value.normalize(), "f")
    return str(value)


def _use_ad(use: dict[str, Any]) -> str:
    return f"ad:{use['advertiser_id']}:{use['ad_remote_id']}"


def _materials_by_ad(inputs: Inputs) -> dict[str, list[Entity]]:
    """广告 → 使用的素材，一次建好索引，避免每个命中都扫描全部素材。"""
    result: dict[str, list[Entity]] = defaultdict(list)
    for entity in inputs.entities.values():
        if entity.kind == "material":
            for use in entity.material_uses:
                result[_use_ad(use)].append(entity)
    return result


def _children(inputs: Inputs) -> dict[str, list[Entity]]:
    result = defaultdict(list)
    for entity in inputs.entities.values():
        if entity.parent_key:
            result[entity.parent_key].append(entity)
    return result


def _targets(
    members: list[Entity],
    level: str,
    inputs: Inputs,
    children: dict[str, list[Entity]],
    materials_by_ad: dict[str, list[Entity]],
) -> tuple[Target, ...]:
    """把判断对象展开到执行层级：执行层级更高时沿上级查找（合计口径），
    更低时向下展开；素材按使用它的广告归属。"""
    found: dict[str, Entity] = {}
    # 由广告展开到素材时只保留命中广告里的使用：同一素材被本账户多条广告共用时，
    # 按广告判断不能把其他广告里的这条素材一起关停。直接判断素材时为 None，取全部使用。
    narrowed: dict[str, list[dict[str, Any]] | None] = {}
    ad_keys: set[str] = set()
    stack = list(members)
    while stack:
        item = stack.pop()
        if item.kind == level:
            found[item.key] = item
            narrowed[item.key] = None
            continue
        if item.kind == "ad":
            ad_keys.add(item.key)
        if level != "material" and _DEPTH[level] < _DEPTH[item.kind]:
            parent = inputs.entities.get(item.parent_key or "")
            if parent is not None:
                stack.append(parent)
            continue
        stack.extend(children.get(item.key, ()))
    if level == "material":
        for ad_key in sorted(ad_keys):
            for material in materials_by_ad.get(ad_key, ()):
                found[material.key] = material
                uses = narrowed.setdefault(material.key, [])
                if uses is not None:
                    uses.extend(
                        u for u in material.material_uses if _use_ad(u) == ad_key
                    )
    return tuple(
        Target(
            e.key,
            e.kind,
            e.advertiser_id,
            e.bc_id,
            e.remote_id,
            e.name,
            e.material_uses
            if narrowed.get(e.key) is None
            else tuple(narrowed[e.key] or ()),
        )
        for e in sorted(found.values(), key=lambda e: e.key)
    )


def evaluate(
    definition: RuleDefinition,
    inputs: Inputs,
    *,
    ignore_status: bool = False,
    strict_facts: bool = False,
) -> Evaluation:
    """ignore_status 只用于反向冲突判断：被规则暂停（或启用）后的对象已不在它的
    作用状态内，但仍要看它的条件按当前数据是否成立。"""
    scoped = sorted(
        (
            e
            for e in inputs.entities.values()
            if _in_scope(e, definition, ignore_status)
        ),
        key=lambda e: e.key,
    )
    skipped = 0
    groups: dict[tuple[str, ...], list[Entity]] = defaultdict(list)
    for entity in scoped:
        if definition.aggregation == "EACH":
            groups[(entity.key,)].append(entity)
        elif definition.aggregation == "DRAMA":
            if not entity.naming_valid or entity.drama is None:
                skipped += 1  # 命名不规范,解析不出剧名,不参与剧合计。
                continue
            groups[(entity.drama, entity.currency)].append(entity)
        elif definition.aggregation == "VIDEO":
            if entity.video is None:
                skipped += 1  # 对不上视频的素材(如旧帖子尚未定位),不参与视频合计。
                continue
            # 不同币种的金额不能相加，同一视频按币种分别合计。
            groups[(entity.video, entity.currency)].append(entity)
        else:
            groups[(entity.currency,)].append(entity)
    children = _children(inputs)
    materials_by_ad = _materials_by_ad(inputs)
    hits = []
    unknown_targets: set[str] = set()
    checked = 0
    missing_video_account = definition.aggregation == "VIDEO" and any(
        item.get("reason") == "STALE_REPORT" for item in inputs.skipped_accounts
    )
    for key, members in groups.items():
        if missing_video_account or any(member.judgement_issue for member in members):
            # VIDEO 合计缺少任何成员的数据时，不能只用其余成员判断整个视频。
            skipped += (
                len(members)
                if missing_video_account
                else sum(bool(member.judgement_issue) for member in members)
            )
            unknown_targets.update(
                t.key
                for t in _targets(
                    members, definition.action.level, inputs, children, materials_by_ad
                )
            )
            continue
        checked += 1
        values = [
            (c, _metric(c, members, inputs, strict_facts=strict_facts))
            for c in definition.conditions
        ]
        if not all(_matches(c, v) for c, v in values):
            # 已知不成立足以排除；其余条件成立但关键值缺失时，不能断言不命中。
            # 普通判断仍不生成命中，反向冲突重算用此证据保持本轮不执行。
            if any(v is None for _, v in values) and all(
                v is None or _matches(c, v) for c, v in values
            ):
                unknown_targets.update(
                    t.key
                    for t in _targets(
                        members,
                        definition.action.level,
                        inputs,
                        children,
                        materials_by_ad,
                    )
                )
            continue
        first = members[0]
        targets = _targets(
            members, definition.action.level, inputs, children, materials_by_ad
        )
        if definition.aggregation == "EACH":
            subject, label = first.key, first.name
        elif definition.aggregation == "DRAMA":
            subject, label = f"drama:{key[0]}:{key[1]}", key[0]
        elif definition.aggregation == "VIDEO":
            subject, label = f"video:{key[0]}:{key[1]}", first.name
        else:
            subject, label = f"all:{key[0]}", f"全部对象({key[0]})"
        hits.append(
            Hit(
                subject_key=subject,
                subject_label=label,
                drama=first.drama,
                advertiser_id=first.advertiser_id
                if definition.aggregation == "EACH"
                else None,
                bc_id=first.bc_id if definition.aggregation == "EACH" else None,
                currency=first.currency,
                metrics={_metric_key(c): _format(v) for c, v in values},
                targets=targets[:MAX_STORED_TARGETS],
                target_count=len(targets),
            )
        )
    return Evaluation(
        checked=checked,
        hits=tuple(hits),
        skipped_count=skipped,
        unknown_targets=tuple(sorted(unknown_targets)),
    )
