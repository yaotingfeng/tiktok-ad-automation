"""以不可拆分创意单元规划两层分配，最后复制文案；计数和实际计划共用规则。"""

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, localcontext
from random import Random
from typing import Protocol
from uuid import UUID

from app.core.errors import DomainError
from app.modules.strategies.copy_pool import CopyChoice
from app.modules.strategies.schemas import (
    MAX_FIXED_ADS_PER_GROUP,
    MAX_FIXED_GROUP_COUNT,
    MAX_TOTAL_PLANNED_ADS,
    StrategyConfig,
)

MAX_UNITS_PER_AD = 50


class NamedCreativeUnit(Protocol):
    @property
    def unit_id(self) -> UUID: ...


@dataclass(frozen=True)
class AdCreativePlan:
    base_ad_no: int
    unit_ids: tuple[UUID, ...]
    copies: tuple[CopyChoice, ...]


@dataclass(frozen=True)
class GroupCreativePlan:
    group_no: int
    unit_ids: tuple[UUID, ...]
    ads: tuple[AdCreativePlan, ...]


@dataclass(frozen=True)
class StructureEstimate:
    campaign_count: int
    adgroup_count: int
    ad_count: int
    daily_budget_sum: Decimal
    # 每个剧目的广告组，每组按基础广告展示单元数；与最终文案版本分开。
    dramas: tuple[tuple[tuple[int, ...], ...], ...]


def _sizes(
    count: int,
    *,
    mode: str,
    fixed: int | None,
    allocation: str | None,
    maximum: int | None,
    fixed_limit: int,
) -> tuple[int, ...]:
    if mode == "FIXED":
        if allocation not in {"SHARED", "SEQUENTIAL_AVERAGE"}:
            raise DomainError(
                "configuration_invalid", "固定数量必须指定创意单元分配方式"
            )
        if fixed is None or fixed > fixed_limit:
            raise DomainError("configuration_invalid", "固定数量超过本地安全上限")
        if allocation == "SEQUENTIAL_AVERAGE" and fixed > count:
            raise DomainError(
                "invalid_material_allocation", "invalid_material_allocation"
            )
        if not count:
            return ()
        if allocation == "SHARED":
            return (count,) * fixed
        width, remainder = divmod(count, fixed)
        return tuple(width + (i < remainder) for i in range(fixed))
    assert maximum is not None
    width, remainder = divmod(count, maximum)
    return (maximum,) * width + ((remainder,) if remainder else ())


def _structure_sizes(count: int, config: StrategyConfig) -> tuple[tuple[int, ...], ...]:
    group_sizes = _sizes(
        count,
        mode=config.group_generation_mode,
        fixed=config.group_count,
        allocation=config.group_material_allocation,
        maximum=config.max_materials_per_group,
        fixed_limit=MAX_FIXED_GROUP_COUNT,
    )
    groups: list[tuple[int, ...]] = []
    total = 0
    for size in group_sizes:
        ads = _sizes(
            size,
            mode=config.ad_generation_mode,
            fixed=config.ads_per_group,
            allocation=config.ad_material_allocation,
            maximum=config.max_materials_per_ad,
            fixed_limit=MAX_FIXED_ADS_PER_GROUP,
        )
        total += len(ads) * config.creative_count
        if total > MAX_TOTAL_PLANNED_ADS:
            raise DomainError(
                "configuration_invalid", "策略展开广告数量超过本地安全上限"
            )
        if any(value > MAX_UNITS_PER_AD for value in ads):
            raise DomainError("creative_count_exceeded", "单广告最多 50 个创意单元")
        groups.append(ads)
    return tuple(groups)


def estimate_batch_structure(
    unit_counts: Iterable[int], *, account_count: int, config: StrategyConfig
) -> StructureEstimate:
    """在账户笛卡尔积/广告对象分配前校验整批；不读取图片成员，不倍增预算。"""
    if type(account_count) is not int or account_count < 0:
        raise ValueError("Invalid account count")
    if account_count == 0:
        return StructureEstimate(0, 0, 0, Decimal(0), ())
    campaigns = groups = ads = 0
    dramas = []
    for count in unit_counts:
        if type(count) is not int or count < 0:
            raise ValueError("Invalid unit count")
        sizes = _structure_sizes(count, config)
        dramas.append(sizes)
        campaigns += bool(sizes) * account_count
        groups += len(sizes) * account_count
        ads += (
            sum(len(group) for group in sizes) * config.creative_count * account_count
        )
        if ads > MAX_TOTAL_PLANNED_ADS:
            raise DomainError(
                "configuration_invalid", "全批预计广告超过 10,000 条，请拆批"
            )
    budget_units = groups if config.budget_strategy == "ADGROUP" else campaigns
    # Money 最多 38 位，再乘工程上限，不能受 Decimal 默认 28 位精度截断。
    with localcontext() as arithmetic:
        arithmetic.prec = 50
        budget = config.budget * budget_units
    return StructureEstimate(campaigns, groups, ads, budget, tuple(dramas))


def _slices(ids: tuple[UUID, ...], sizes: tuple[int, ...], shared: bool):
    offset = 0
    for size in sizes:
        yield ids if shared else ids[offset : offset + size]
        offset += size


def plan_structure(
    units: Iterable[NamedCreativeUnit],
    *,
    config: StrategyConfig,
    pool: tuple[CopyChoice, ...],
    seed: int,
) -> tuple[GroupCreativePlan, ...]:
    # 单元身份去重：相同图片不同曲目、明确复制的单元不可按素材或内容摘要合并。
    unit_ids = tuple(dict.fromkeys(item.unit_id for item in units))
    unique: dict[str, CopyChoice] = {}
    for choice in pool:
        if choice.text.strip() and choice.text not in unique:
            unique[choice.text] = choice
    copies = tuple(unique.values())
    if config.creative_count > len(copies):
        raise DomainError("copy_pool_exhausted", "copy_pool_exhausted")
    sizes = _structure_sizes(len(unit_ids), config)
    group_sizes = _sizes(
        len(unit_ids),
        mode=config.group_generation_mode,
        fixed=config.group_count,
        allocation=config.group_material_allocation,
        maximum=config.max_materials_per_group,
        fixed_limit=MAX_FIXED_GROUP_COUNT,
    )
    rng = Random(seed)
    groups = []
    for group_no, (ids, ad_sizes) in enumerate(
        zip(
            _slices(
                unit_ids, group_sizes, config.group_material_allocation == "SHARED"
            ),
            sizes,
            strict=True,
        ),
        1,
    ):
        ads = tuple(
            AdCreativePlan(
                base_ad_no, ad_ids, tuple(rng.sample(copies, config.creative_count))
            )
            for base_ad_no, ad_ids in enumerate(
                _slices(ids, ad_sizes, config.ad_material_allocation == "SHARED"), 1
            )
        )
        groups.append(GroupCreativePlan(group_no, ids, ads))
    return tuple(groups)
