from string import Formatter

from app.core.errors import DomainError

DEFAULT_NAME_TEMPLATE = "{provider_pinyin}-{drama_name}-{drama_id}-{random}"
NAME_FIELDS = frozenset(
    {"provider_pinyin", "drama_name", "drama_id", "random", "YYYYMMDD"}
)


def _validate_template(
    value: str, *, allowed: frozenset[str], required: set[str]
) -> None:
    try:
        parts = tuple(Formatter().parse(value))
    except ValueError, TypeError:
        raise DomainError("invalid_name_template", "invalid_name_template") from None
    fields = {field for _, field, _, _ in parts if field is not None}
    if (
        not value.strip()
        or len(value) > 1000
        or fields - allowed
        or not required.issubset(fields)
        or any(spec or conversion for _, _, spec, conversion in parts)
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise DomainError("invalid_name_template", "invalid_name_template")


def validate_suffix(suffix: str) -> None:
    _validate_template(
        suffix,
        allowed=frozenset({"YYYYMMDD", "batch_short_id"}),
        required={"batch_short_id"},
    )


def validate_name_template(template: str) -> None:
    # 剧目 ID 区分同名剧，随机批次号区分重复搭建；不接受属性/下标等格式表达式。
    _validate_template(template, allowed=NAME_FIELDS, required={"drama_id", "random"})


def render_names(
    *,
    protected_base: str,
    title: str,
    date_text: str,
    batch_short_id: str,
    suffix: str,
    group_no: int,
    creative_no: int,
    max_length: int,
    provider_pinyin: str,
    external_drama_id: str,
    template: str = DEFAULT_NAME_TEMPLATE,
) -> tuple[str, str, str]:
    validate_suffix(suffix)
    validate_name_template(template)
    if any(
        type(value) is not int or value < 1
        for value in (group_no, creative_no, max_length)
    ):
        raise DomainError("invalid_group_config", "invalid_group_config")
    # 已核验的版权方归因名优先；插入值不会再次作为模板解析。
    if protected_base:
        campaign = protected_base + suffix.format_map(
            {"YYYYMMDD": date_text, "batch_short_id": batch_short_id}
        )
    else:
        if (
            not provider_pinyin.strip()
            or not external_drama_id.strip()
            or not title.strip()
        ):
            raise DomainError("naming_context_missing", "缺少版权方或剧目标识")
        campaign = template.format_map(
            {
                "provider_pinyin": provider_pinyin,
                "drama_name": title,
                "drama_id": external_drama_id,
                "random": batch_short_id,
                "YYYYMMDD": date_text,
            }
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
