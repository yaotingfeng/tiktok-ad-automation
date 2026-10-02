"""Provider-specific link configuration schemas exposed to the build UI."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class LinkConfigField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1, max_length=255)
    type: Literal["string", "integer", "select", "boolean"]
    required: bool = False
    default: Any = None
    options: list[str] = Field(default_factory=list)


class LinkConfigSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_kind: str = Field(min_length=1, max_length=32)
    fields: list[LinkConfigField] = Field(max_length=50)
    defaults: dict[str, Any] = Field(default_factory=dict)
    required_when: dict[str, dict[str, list[str]]] = Field(default_factory=dict)
    schema_version: int = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def validate_field_references(self) -> LinkConfigSchema:
        names = [field.name for field in self.fields]
        if len(names) != len(set(names)):
            raise ValueError("capability field names must be unique")
        known = set(names)
        if set(self.defaults) - known or set(self.required_when) - known:
            raise ValueError("capability defaults must reference declared fields")
        return self


def capabilities_for_kind(provider_kind: str, _application: Any = None) -> LinkConfigSchema:
    """Return the safe, UI-facing link configuration contract for a provider."""

    if provider_kind == "jiashu":
        fields = [LinkConfigField(name="episode", label="起播集", type="integer", default=1)]
    elif provider_kind == "wangyan":
        fields = [
            LinkConfigField(name="episode", label="起播集", type="integer", default=1),
            LinkConfigField(name="promote_name", label="推广名称", type="string"),
        ]
    elif provider_kind == "duiba":
        fields = [
            LinkConfigField(name="episode", label="起播集", type="integer", default=1),
            LinkConfigField(
                name="card_point_episode", label="卡点集", type="integer", default=1
            ),
            LinkConfigField(name="miniapp_id", label="小程序", type="string"),
        ]
    elif provider_kind == "gangganhao":
        channel_config = getattr(_application, "channel_config", {})
        delivery_mode = (
            str(channel_config.get("delivery_mode", "iaa")).lower()
            if isinstance(channel_config, dict)
            else "iaa"
        )
        fields = [
            LinkConfigField(
                name="free_episode_count", label="免费集数", type="integer", default=1
            ),
            LinkConfigField(name="episode_seq", label="跳转集", type="integer", default=1),
            LinkConfigField(
                name="payment_template_id",
                label="支付模板",
                type="select",
                required=delivery_mode in {"iap", "mixed"},
            ),
            LinkConfigField(name="name", label="推广名称", type="string"),
        ]
    elif provider_kind == "rongliang":
        fields = [
            LinkConfigField(name="episodic_drama_id", label="剧集", type="string"),
            LinkConfigField(name="alias", label="别名", type="string"),
        ]
    else:
        raise ValueError("unknown provider kind")
    defaults = {field.name: field.default for field in fields if field.default is not None}
    required_when = (
        {"payment_template_id": {"delivery_mode": ["iap", "mixed"]}}
        if provider_kind == "gangganhao"
        else {}
    )
    return LinkConfigSchema(
        provider_kind=provider_kind,
        fields=fields,
        defaults=defaults,
        required_when=required_when,
        schema_version=1,
    )
