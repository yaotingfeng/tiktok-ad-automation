"""将不可变的历史策略配置投影为当前编辑/新预览格式，不改写历史记录。"""

from string import Formatter
from typing import Any

from app.modules.strategies.schemas import StrategyConfig


def read_saved_config(saved: dict[str, Any]) -> StrategyConfig:
    config = dict(saved)
    # General strategy changed the meaning of both legacy quantities.  Keep the
    # immutable JSON untouched and project it only at this read boundary:
    # ``group_size`` becomes a material-bounded group rule, while the old
    # creative count becomes fixed ads sharing each group's materials.
    legacy_structure = "group_size" in config or (
        "creative_count" in config and "ad_generation_mode" not in config
    )
    if legacy_structure:
        group_size = config.pop("group_size", 1)
        old_creative_count = config.pop("creative_count", 1)
        config.setdefault("budget_strategy", "SERIES")
        config["bid_strategy"] = (
            "TARGET_ROAS" if config.get("target_roas") is not None else "HIGHEST_VALUE"
        )
        config["group_generation_mode"] = "BY_MATERIAL"
        config["group_count"] = None
        config["group_material_allocation"] = None
        config["max_materials_per_group"] = group_size
        config["ad_generation_mode"] = "FIXED"
        config["ads_per_group"] = old_creative_count
        config["ad_material_allocation"] = "SHARED"
        config["max_materials_per_ad"] = None
        config["creative_count"] = 1
    if "campaign_suffix" in config:
        # 旧后缀退出策略契约；仅转换存储格式，不保留旧版权方命名引擎。
        # 已冻结预览和提交直接读名称快照，不经过此转换。
        config.pop("campaign_suffix")
        template = config.get(
            "campaign_name_template",
            "{provider_pinyin}-{drama_name}-{drama_id}-{random}",
        )
        parts: list[str] = []
        for literal, field, _, _ in Formatter().parse(template):
            literal = literal.replace("{", "{{").replace("}", "}}")
            if field in {"provider_pinyin", "drama_name", "random"}:
                parts.append(literal.removesuffix("-"))
                continue
            parts.append(literal + (f"{{{field}}}" if field else ""))
        # 归因基础名始终在开头；其后保留通用的日期、剧目 ID 与固定文字。
        config["campaign_name_template"] = "{provider_drama}-" + "".join(
            parts
        ).removeprefix("-")
    return StrategyConfig.model_validate(config)
