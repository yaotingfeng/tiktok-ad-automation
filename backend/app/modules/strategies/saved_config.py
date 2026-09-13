"""将不可变的历史策略配置投影为当前编辑/新预览格式，不改写历史记录。"""

from string import Formatter
from typing import Any

from app.modules.strategies.schemas import StrategyConfig


def read_saved_config(saved: dict[str, Any]) -> StrategyConfig:
    config = dict(saved)
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
