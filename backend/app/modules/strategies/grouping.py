"""素材结构规划的兼容导出。

分组与广告素材分配已统一由 :mod:`structure` 实现；保留本模块路径，方便尚未
迁移的只读调用方导入新的计划类型，不再提供旧的 ``group_size`` 语义。
"""

from app.modules.strategies.structure import (
    AdMaterialPlan,
    GroupPlan,
    NamedMaterial,
    plan_structure,
)

__all__ = ["AdMaterialPlan", "GroupPlan", "NamedMaterial", "plan_structure"]
