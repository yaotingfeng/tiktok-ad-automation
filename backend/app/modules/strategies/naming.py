from string import Formatter

from app.core.errors import DomainError


def validate_suffix(suffix: str) -> None:
    try:
        parts = tuple(Formatter().parse(suffix))
    except ValueError, TypeError:
        raise DomainError("invalid_name_template", "invalid_name_template") from None
    fields = {field for _, field, _, _ in parts if field is not None}
    if (
        fields - {"YYYYMMDD", "batch_short_id"}
        or "batch_short_id" not in fields
        or any(spec or conversion for _, _, spec, conversion in parts)
        or any(ord(char) < 32 or ord(char) == 127 for char in suffix)
    ):
        raise DomainError("invalid_name_template", "invalid_name_template")


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
) -> tuple[str, str, str]:
    validate_suffix(suffix)
    if any(
        type(value) is not int or value < 1
        for value in (group_no, creative_no, max_length)
    ):
        raise DomainError("invalid_group_config", "invalid_group_config")
    base = protected_base if protected_base else title
    campaign = base + suffix.format_map(
        {"YYYYMMDD": date_text, "batch_short_id": batch_short_id}
    )
    group = f"{campaign}-g{group_no:02d}"
    ad = f"{group}-sp{creative_no}"
    if (
        not base.strip()
        or not batch_short_id.strip()
        or any(len(name) > max_length for name in (campaign, group, ad))
    ):
        raise DomainError("name_too_long", "name_too_long")
    return campaign, group, ad
