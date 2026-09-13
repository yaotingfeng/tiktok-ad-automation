from string import Formatter

from app.core.errors import DomainError

DEFAULT_NAME_TEMPLATE = "{provider_drama}-{drama_id}"
NAME_FIELDS = frozenset({"provider_drama", "drama_id", "YYYYMMDD"})


def validate_name_template(template: str) -> None:
    # 版权方与剧名作为整体，确保专用归因基础名只替换这一部分。
    try:
        parts = tuple(Formatter().parse(template))
    except ValueError, TypeError:
        raise DomainError("invalid_name_template", "invalid_name_template") from None
    fields = [field for _, field, _, _ in parts if field is not None]
    if (
        not template.startswith("{provider_drama}")
        or len(template) > 1000
        or set(fields) - NAME_FIELDS
        or not {"provider_drama", "drama_id"}.issubset(fields)
        or len(fields) != len(set(fields))
        or any(spec or conversion for _, _, spec, conversion in parts)
        or any(ord(char) < 32 or ord(char) == 127 for char in template)
    ):
        raise DomainError("invalid_name_template", "invalid_name_template")


def render_names(
    *,
    protected_base: str,
    title: str,
    date_text: str,
    batch_short_id: str,
    group_no: int,
    creative_no: int,
    max_length: int,
    provider_pinyin: str,
    external_drama_id: str,
    template: str = DEFAULT_NAME_TEMPLATE,
) -> tuple[str, str, str]:
    validate_name_template(template)
    if any(
        type(value) is not int or value < 1
        for value in (group_no, creative_no, max_length)
    ):
        raise DomainError("invalid_group_config", "invalid_group_config")
    if not external_drama_id.strip() or (
        not protected_base and (not provider_pinyin.strip() or not title.strip())
    ):
        raise DomainError("naming_context_missing", "缺少版权方或剧目标识")
    # 网眼仅替换“版权方＋剧名”；剧目 ID、日期及固定文字共用同一模板。
    # 插入值是字面文本，归因花括号不会被再次解析。批次编号由系统统一追加。
    campaign = (
        template.format_map(
            {
                "provider_drama": protected_base or f"{provider_pinyin}-{title}",
                "drama_id": external_drama_id,
                "YYYYMMDD": date_text,
            }
        )
        + f"-{batch_short_id}"
    )
    group = f"{campaign}-g{group_no:02d}"
    ad = f"{group}-sp{creative_no}"
    if (
        not campaign.strip()
        or not batch_short_id.strip()
        or any(len(name) > max_length for name in (campaign, group, ad))
    ):
        raise DomainError("name_too_long", "name_too_long")
    return campaign, group, ad
