"""Minis 严格定向选择；国家 ID 由账户场景解析，策略不保存跨账户 ID。"""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.errors import DomainError

# 官方 smart_plus/adgroup/create 文档与枚举，核实于 2026-09-28。
# AGE_13_17 仅支持 APP_INSTALL/APP_RETARGETING，不属于本工具的 Minis 场景。
AgeGroup = Literal["AGE_18_24", "AGE_25_34", "AGE_35_44", "AGE_45_54", "AGE_55_100"]
Gender = Literal["GENDER_UNLIMITED", "GENDER_MALE", "GENDER_FEMALE"]
Language = Literal[
    "ar",
    "as",
    "bgc",
    "bh",
    "bn",
    "cs",
    "de",
    "el",
    "en",
    "es",
    "fi",
    "fr",
    "gu",
    "he",
    "hi",
    "hu",
    "id",
    "it",
    "ja",
    "kn",
    "ko",
    "ml",
    "mr",
    "ms",
    "nl",
    "or",
    "pa",
    "pl",
    "pt",
    "raj",
    "ro",
    "ru",
    "sv",
    "ta",
    "te",
    "th",
    "tr",
    "uk",
    "vi",
    "zh",
    "zh-Hant",
]
Country = Annotated[str, Field(strict=True, pattern=r"^[A-Z]{2}$")]


class AudienceTargeting(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    region_mode: Literal["ALL_AVAILABLE", "SELECTED"] = "ALL_AVAILABLE"
    region_codes: tuple[Country, ...] = Field(default=(), max_length=300)
    languages: tuple[Language, ...] = Field(default=(), max_length=41)
    age_groups: tuple[AgeGroup, ...] = Field(default=(), max_length=5)
    gender: Gender = "GENDER_UNLIMITED"

    @field_validator("region_codes", "languages", "age_groups")
    @classmethod
    def canonical_set(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(value)))

    @model_validator(mode="after")
    def selected_regions(self) -> Self:
        if (self.region_mode == "SELECTED") != bool(self.region_codes):
            raise ValueError("指定国家不能为空；全部可投模式不得携带国家选择")
        return self

    def automatic_minimum_age(self) -> Literal["OVER_EIGHTEEN", "OVER_TWENTY_FIVE"]:
        """保留历史选择供展示；只将语义完整等价的成人年龄集合编译为最低年龄。"""
        if self.gender != "GENDER_UNLIMITED":
            raise DomainError(
                "targeting_minis_unsupported",
                "小程序自动定向不支持严格性别限制，请重新选择不限性别",
            )
        ages = set(self.age_groups)
        over_25 = {"AGE_25_34", "AGE_35_44", "AGE_45_54", "AGE_55_100"}
        if not ages or ages == over_25 | {"AGE_18_24"}:
            return "OVER_EIGHTEEN"
        if ages == over_25:
            return "OVER_TWENTY_FIVE"
        raise DomainError(
            "targeting_minis_unsupported",
            "小程序自动定向仅支持18岁以上或25岁以上，请重新选择最低年龄",
        )


class TargetingChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    expected_revision: int = Field(gt=0, strict=True)
    targeting_override: AudienceTargeting | None


class TargetingDirectory(BaseModel):
    region_codes: list[str] = Field(default_factory=list)
    account_count: int = 0
    verified_account_count: int = 0
    unavailable_region_codes: list[str] = Field(default_factory=list)
    state: Literal["READY", "PENDING", "UNAVAILABLE"] = "PENDING"
    revision: int | None = None


class TargetingAccount(BaseModel):
    advertiser_id: str
    region_codes: list[str]
    reason_codes: list[str]
