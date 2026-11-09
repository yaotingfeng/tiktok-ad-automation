"""预览定向解析是纯函数；绝不在执行时读取策略或扩大冻结国家集合。"""

from dataclasses import replace

from app.core.errors import DomainError

from .scene_schemas import SceneContext
from .targeting_schemas import AudienceTargeting


def apply_targeting(
    scene: SceneContext, selected: AudienceTargeting, common: list[str]
) -> SceneContext:
    mapping = {
        item["region_code"]: item["location_id"]
        for item in scene.field_constraints.get("target_regions", ())
    }
    countries = (
        list(selected.region_codes)
        if selected.region_mode == "SELECTED"
        else sorted(set(common))
    )
    reasons = list(scene.reason_codes)
    if (
        not countries
        or not set(countries) <= set(mapping)
        or not set(countries) <= set(common)
    ):
        reasons.append("targeting_regions_unavailable")
    group = dict(scene.adgroup_fields)
    # 专用Mini Dramas Non-Catalog合同只允许自动模式；语言控制不触发MANUAL。
    # 旧精细年龄/性别条件必须明确修正，不能悄悄扩大受众后继续创建。
    try:
        minimum_age = selected.automatic_minimum_age()
    except DomainError:
        reasons.append("targeting_minis_unsupported")
        minimum_age = None
    group["targeting_optimization_mode"] = "AUTOMATIC"
    group["targeting_spec"] = {
        "location_ids": sorted(mapping[code] for code in countries if code in mapping),
        **({"languages": list(selected.languages)} if selected.languages else {}),
        **({"spc_audience_age": minimum_age} if minimum_age else {}),
    }
    return replace(
        scene,
        supported=scene.supported and not reasons,
        reason_codes=tuple(sorted(set(reasons))),
        adgroup_fields=group,
        field_constraints={
            **scene.field_constraints,
            "audience_targeting": selected.model_dump(mode="json"),
            "selected_region_codes": countries,
        },
    )
