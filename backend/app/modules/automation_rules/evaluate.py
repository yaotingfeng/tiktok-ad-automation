"""纯函数判断引擎:按统计口径分组、计算指标、判断条件、展开执行对象。"""

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from app.modules.automation_rules.catalog import RULE_METRICS
from app.modules.automation_rules.inputs import Entity, Inputs
from app.modules.automation_rules.schemas import Condition, RuleDefinition
from app.modules.reporting.metric_catalog import DERIVED_RATIOS, STORED_KEYS

_RATIOS = {name: (num, den, scale) for name, num, den, scale in DERIVED_RATIOS}
_STATUS = {"ACTIVE": "ENABLE", "PAUSED": "DISABLE"}


@dataclass(frozen=True)
class Target:
    key: str
    kind: str
    advertiser_id: str
    bc_id: str
    remote_id: str
    name: str
    material_uses: tuple[dict, ...] = ()


@dataclass(frozen=True)
class Hit:
    subject_key: str
    subject_label: str
    drama: str | None
    advertiser_id: str | None
    bc_id: str | None
    currency: str
    metrics: dict[str, str]
    targets: tuple[Target, ...]


@dataclass(frozen=True)
class Evaluation:
    checked: int
    hits: tuple[Hit, ...]
    skipped_count: int


def _in_scope(entity: Entity, definition: RuleDefinition) -> bool:
    apply = definition.apply_to
    if entity.kind != apply.level:
        return False
    if apply.status == "SELECTED":
        if f"{entity.advertiser_id}:{entity.remote_id}" not in apply.object_ids:
            return False
    elif entity.status != _STATUS[apply.status]:
        return False
    if apply.drama_names and entity.drama not in apply.drama_names:
        return False
    if apply.name_contains and apply.name_contains not in entity.name:
        return False
    if apply.name_excludes and apply.name_excludes in entity.name:
        return False
    return True


def _sum(members, facts, range_type):
    totals: dict[str, Decimal | None] = {key: Decimal(0) for key in STORED_KEYS}
    for member in members:
        row = facts.get(range_type, {}).get(member.key)
        if row is None:
            continue  # 已通过新鲜度检查的账户,没有事实行即没有消耗,按 0 计。
        for key in STORED_KEYS:
            value = row.get(key)
            if value is not None:
                totals[key] = (totals[key] or Decimal(0)) + value
    return totals


def _metric(condition: Condition, members, inputs) -> Decimal | str | None:
    meta = RULE_METRICS[condition.metric]
    if meta.kind == "attribute":
        return members[0].attributes.get(condition.metric)
    totals = _sum(members, inputs.facts, condition.range)
    if condition.metric in _RATIOS:
        num, den, scale = _RATIOS[condition.metric]
        a, b = totals.get(num), totals.get(den)
        # 分母为 0 或缺失时没有比率,条件视为不满足。
        return a / b * scale if a is not None and b else None
    return totals.get(condition.metric)


def _matches(condition: Condition, value) -> bool:
    if value is None:
        return False
    op, values = condition.operator, condition.values
    if isinstance(value, str):
        target = values[0]
        return {
            "CONTAINS": target in value,
            "NOT_CONTAINS": target not in value,
            "STARTS_WITH": value.startswith(target),
            "ENDS_WITH": value.endswith(target),
            "EQ": value == target,
        }[op]
    numbers = [Decimal(v) for v in values]
    if op == "GT":
        return value > numbers[0]
    if op == "LT":
        return value < numbers[0]
    if op == "EQ":
        return value == numbers[0]
    return numbers[0] < value < numbers[1]


def _metric_key(condition: Condition) -> str:
    return (
        f"{condition.metric}:{condition.range}" if condition.range else condition.metric
    )


def _format(value) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        # 比率保留 4 位小数；去掉多余的 0，避免出现科学计数法。
        if value != value.to_integral():
            value = value.quantize(Decimal("0.0001"))
        return format(value.normalize(), "f")
    return str(value)


def _children(inputs: Inputs) -> dict[str, list[Entity]]:
    result = defaultdict(list)
    for entity in inputs.entities.values():
        if entity.parent_key:
            result[entity.parent_key].append(entity)
    return result


def _targets(members, level, inputs, children) -> tuple[Target, ...]:
    """把判断对象展开到执行层级;素材按使用它的广告归属。"""
    found: dict[str, Entity] = {}
    ad_keys: set[str] = set()
    stack = list(members)
    while stack:
        item = stack.pop()
        if item.kind == level:
            found[item.key] = item
            continue
        if item.kind == "ad":
            ad_keys.add(item.key)
        stack.extend(children.get(item.key, ()))
    if level == "material":
        for entity in inputs.entities.values():
            if entity.kind == "material" and (
                entity.key in found
                or any(
                    f"ad:{u['advertiser_id']}:{u['ad_remote_id']}" in ad_keys
                    for u in entity.material_uses
                )
            ):
                found[entity.key] = entity
    return tuple(
        Target(
            e.key,
            e.kind,
            e.advertiser_id,
            e.bc_id,
            e.remote_id,
            e.name,
            e.material_uses,
        )
        for e in sorted(found.values(), key=lambda e: e.key)
    )


def evaluate(definition: RuleDefinition, inputs: Inputs) -> Evaluation:
    scoped = sorted(
        (e for e in inputs.entities.values() if _in_scope(e, definition)),
        key=lambda e: e.key,
    )
    skipped = 0
    groups: dict[tuple, list[Entity]] = defaultdict(list)
    for entity in scoped:
        if definition.aggregation == "EACH":
            groups[(entity.key,)].append(entity)
        elif definition.aggregation == "DRAMA":
            if not entity.naming_valid or entity.drama is None:
                skipped += 1  # 命名不规范,解析不出剧名,不参与剧合计。
                continue
            groups[(entity.drama, entity.currency)].append(entity)
        else:
            groups[(entity.currency,)].append(entity)
    children = _children(inputs)
    hits = []
    for key, members in groups.items():
        values = [(c, _metric(c, members, inputs)) for c in definition.conditions]
        if not all(_matches(c, v) for c, v in values):
            continue
        first = members[0]
        if definition.aggregation == "EACH":
            subject, label = first.key, first.name
        elif definition.aggregation == "DRAMA":
            subject, label = f"drama:{key[0]}:{key[1]}", key[0]
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
                targets=_targets(members, definition.action.level, inputs, children),
            )
        )
    return Evaluation(checked=len(groups), hits=tuple(hits), skipped_count=skipped)
