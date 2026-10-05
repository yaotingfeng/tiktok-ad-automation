"""纯素材结构规划。

规划器只根据已经确认的素材和策略配置生成内存计划；它不查询数据库或平台，
也不会在素材不足时偷偷补素材。广告创意复制放在两层素材分配完成之后，保证
同一个基础广告的复制广告始终引用完全相同的素材集合。
"""

from collections.abc import Iterable
from dataclasses import dataclass
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


class NamedMaterial(Protocol):
    @property
    def material_id(self) -> UUID: ...

    @property
    def file_name(self) -> str: ...


@dataclass(frozen=True)
class AdMaterialPlan:
    base_ad_no: int
    material_ids: tuple[UUID, ...]
    copies: tuple[CopyChoice, ...]


@dataclass(frozen=True)
class GroupPlan:
    group_no: int
    material_ids: tuple[UUID, ...]
    ads: tuple[AdMaterialPlan, ...]


def _stable_material_ids(materials: Iterable[NamedMaterial]) -> tuple[UUID, ...]:
    """保留素材包冻结顺序，并按首次出现的 ID 去重。"""
    seen: set[UUID] = set()
    result: list[UUID] = []
    for item in materials:
        if item.material_id not in seen:
            seen.add(item.material_id)
            result.append(item.material_id)
    return tuple(result)


def _average_slices(
    material_ids: tuple[UUID, ...], unit_count: int
) -> tuple[tuple[UUID, ...], ...]:
    """把连续素材切成 unit_count 段，余数依次放到前面的段。"""
    if unit_count > len(material_ids):
        raise DomainError("invalid_material_allocation", "invalid_material_allocation")
    width, remainder = divmod(len(material_ids), unit_count)
    result: list[tuple[UUID, ...]] = []
    cursor = 0
    for index in range(unit_count):
        size = width + (1 if index < remainder else 0)
        result.append(material_ids[cursor : cursor + size])
        cursor += size
    return tuple(result)


def _bounded_slices(
    material_ids: tuple[UUID, ...], max_materials: int
) -> tuple[tuple[UUID, ...], ...]:
    """按素材上限依次切片；尾段允许少于上限。"""
    return tuple(
        material_ids[index : index + max_materials]
        for index in range(0, len(material_ids), max_materials)
    )


def _allocate_groups(
    material_ids: tuple[UUID, ...], config: StrategyConfig
) -> tuple[tuple[UUID, ...], ...]:
    if config.group_generation_mode == "FIXED" and (
        config.group_count is None or config.group_count > MAX_FIXED_GROUP_COUNT
    ):
        raise DomainError("configuration_invalid", "固定广告组数量超过本地安全上限")
    if not material_ids:
        # 没有素材时不生成空广告组；平均模式仍需明确报告数量不足。
        if (
            config.group_generation_mode == "FIXED"
            and config.group_material_allocation == "SEQUENTIAL_AVERAGE"
        ):
            assert config.group_count is not None
            return _average_slices(material_ids, config.group_count)
        return ()

    if config.group_generation_mode == "BY_MATERIAL":
        assert config.max_materials_per_group is not None
        return _bounded_slices(material_ids, config.max_materials_per_group)

    assert config.group_count is not None
    if config.group_material_allocation == "SHARED":
        return tuple(material_ids for _ in range(config.group_count))
    assert config.group_material_allocation == "SEQUENTIAL_AVERAGE"
    return _average_slices(material_ids, config.group_count)


def _allocate_ads(
    material_ids: tuple[UUID, ...], config: StrategyConfig
) -> tuple[tuple[UUID, ...], ...]:
    if config.ad_generation_mode == "FIXED" and (
        config.ads_per_group is None or config.ads_per_group > MAX_FIXED_ADS_PER_GROUP
    ):
        raise DomainError("configuration_invalid", "固定每组广告数量超过本地安全上限")
    if not material_ids:
        if (
            config.ad_generation_mode == "FIXED"
            and config.ad_material_allocation == "SEQUENTIAL_AVERAGE"
        ):
            assert config.ads_per_group is not None
            return _average_slices(material_ids, config.ads_per_group)
        return ()

    if config.ad_generation_mode == "BY_MATERIAL":
        assert config.max_materials_per_ad is not None
        return _bounded_slices(material_ids, config.max_materials_per_ad)

    assert config.ads_per_group is not None
    if config.ad_material_allocation == "SHARED":
        return tuple(material_ids for _ in range(config.ads_per_group))
    assert config.ad_material_allocation == "SEQUENTIAL_AVERAGE"
    return _average_slices(material_ids, config.ads_per_group)


def _unique_copies(pool: tuple[CopyChoice, ...]) -> tuple[CopyChoice, ...]:
    """过滤空正文并按正文去重，沿用文案池的稳定顺序。"""
    unique: dict[str, CopyChoice] = {}
    for choice in pool:
        if choice.text.strip() and choice.text not in unique:
            unique[choice.text] = choice
    return tuple(unique.values())


def plan_structure(
    materials: Iterable[NamedMaterial],
    *,
    config: StrategyConfig,
    pool: tuple[CopyChoice, ...],
    seed: int,
) -> tuple[GroupPlan, ...]:
    """生成广告组、基础广告素材集合及创意复制后的纯内存计划。"""
    copies = _unique_copies(pool)
    if config.creative_count > len(copies):
        raise DomainError("copy_pool_exhausted", "copy_pool_exhausted")

    material_ids = _stable_material_ids(materials)
    group_material_sets = _allocate_groups(material_ids, config)
    # 在创建广告/创意 tuple 前先计算最终展开规模，避免固定组数、固定广告数
    # 与创意复制组合成百万级内存计划。该上限高于当前正常批量策略，且不替代
    # 场景阶段对平台单广告组上限的核验。
    planned_ad_count = 0
    for group_ids in group_material_sets:
        if config.ad_generation_mode == "FIXED":
            assert config.ads_per_group is not None
            base_ad_count = config.ads_per_group
        else:
            assert config.max_materials_per_ad is not None
            base_ad_count = (
                len(group_ids) + config.max_materials_per_ad - 1
            ) // config.max_materials_per_ad
        planned_ad_count += base_ad_count * config.creative_count
        if planned_ad_count > MAX_TOTAL_PLANNED_ADS:
            raise DomainError(
                "configuration_invalid", "策略展开广告数量超过本地安全上限"
            )
    rng = Random(seed)
    groups: list[GroupPlan] = []

    for group_no, group_ids in enumerate(group_material_sets, start=1):
        ad_material_sets = _allocate_ads(group_ids, config)
        ads: list[AdMaterialPlan] = []
        for base_ad_no, ad_ids in enumerate(ad_material_sets, start=1):
            # 基础广告先固定素材集合和文案池抽样；后续执行阶段再按 creative_no 展开。
            selected = tuple(rng.sample(copies, config.creative_count))
            ads.append(AdMaterialPlan(base_ad_no, ad_ids, selected))
        groups.append(GroupPlan(group_no, group_ids, tuple(ads)))

    return tuple(groups)
