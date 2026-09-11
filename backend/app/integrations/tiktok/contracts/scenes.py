"""场景业务 DTO；不允许无类型 JSON 穿过读取合同。"""

from typing import Annotated, Literal, Protocol, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from app.modules.builds.scene_schemas import SceneResource

from .accounts import AccountRoleFact
from .common import CallEvidence

Text = Annotated[StrictStr, Field(min_length=1, max_length=255)]
Count = Annotated[StrictInt, Field(ge=0)]


class FrozenFacts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PaginatedFacts(FrozenFacts):
    item_id_hashes: tuple[Text, ...]
    total_number: Count
    total_page: Annotated[StrictInt, Field(ge=0, le=1000)]
    seen: Annotated[StrictInt, Field(ge=0, le=50)]

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if (
            len(set(self.item_id_hashes)) != self.seen
            or len(self.item_id_hashes) != self.seen
            or self.total_number < self.seen
        ):
            raise ValueError("inconsistent scene count")
        matches = getattr(self, "matches", ())
        ids = [
            getattr(item, "advertiser_id", None)
            or getattr(item, "identity_id", None)
            or getattr(item, "minis_id", None)
            for item in matches
        ]
        if len(ids) != len(set(ids)) or len(ids) > self.seen:
            raise ValueError("duplicate scene matches")
        if self.total_page == 0 and (self.total_number != 0 or self.seen != 0):
            raise ValueError("inconsistent empty scene")
        return self


class RoleFacts(PaginatedFacts):
    matches: tuple[AccountRoleFact, ...]


class IdentityMatch(FrozenFacts):
    identity_id: Text
    identity_type: Literal["BC_AUTH_TT"]
    identity_authorized_bc_id: Text


class IdentityFacts(PaginatedFacts):
    matches: tuple[IdentityMatch, ...]


class MinisMatch(FrozenFacts):
    minis_id: Text
    status: Literal["ACTIVE", "INACTIVE"]
    type: Literal["MINI_SERIES", "MINI_GAME"]
    regions: tuple[Annotated[StrictStr, Field(pattern=r"^[A-Z]{2}$")], ...]


class MinisFacts(PaginatedFacts):
    matches: tuple[MinisMatch, ...]


class CtaRecommendation(FrozenFacts):
    asset_ids: tuple[Text, ...]
    asset_content: Text


class CtaFacts(FrozenFacts):
    asset_ids: tuple[Text, ...]
    recommend_assets: tuple[CtaRecommendation, ...]


class VboFacts(FrozenFacts):
    vo_status: Text | None = None
    vo_min_roas: Text | None = None
    roas_status_day0: Text | None = None
    roas_status_day7: Text | None = None

    @model_validator(mode="after")
    def require_fact(self) -> Self:
        if not any(
            value is not None
            for value in (
                self.vo_status,
                self.vo_min_roas,
                self.roas_status_day0,
                self.roas_status_day7,
            )
        ):
            raise ValueError("missing VBO fact")
        return self


class RegionLocation(FrozenFacts):
    region_code: Annotated[StrictStr, Field(pattern=r"^[A-Z]{2}$")]
    location_id: Text


class RegionFacts(FrozenFacts):
    locations: tuple[RegionLocation, ...]

    @model_validator(mode="after")
    def validate_unique(self) -> Self:
        if len({item.region_code for item in self.locations}) != len(
            self.locations
        ) or len({item.location_id for item in self.locations}) != len(self.locations):
            raise ValueError("duplicate scene locations")
        return self


SceneFacts = RoleFacts | IdentityFacts | MinisFacts | CtaFacts | VboFacts | RegionFacts
FACT_TYPES = {
    "account_roles": RoleFacts,
    "identity": IdentityFacts,
    "minis": MinisFacts,
    "cta": CtaFacts,
    "vbo": VboFacts,
    "regions": RegionFacts,
}


class ScenePage(FrozenFacts):
    resource: SceneResource
    page: Annotated[StrictInt, Field(ge=1, le=1000)]
    last: Annotated[bool, Field(strict=True)]
    facts: SceneFacts
    evidence: CallEvidence

    @field_validator("facts", mode="before")
    @classmethod
    def require_typed_facts(cls, value: object) -> object:
        if type(value) not in FACT_TYPES.values():
            raise ValueError("untyped scene facts")
        return value

    @model_validator(mode="after")
    def validate_resource(self) -> Self:
        if type(self.facts) is not FACT_TYPES[self.resource]:
            raise ValueError("scene resource/facts mismatch")
        if isinstance(self.facts, PaginatedFacts):
            if self.page > max(1, self.facts.total_page) or self.last != (
                self.page == max(1, self.facts.total_page)
            ):
                raise ValueError("inconsistent scene page")
            expected_pages = max(1, (self.facts.total_number + 49) // 50)
            expected_seen = min(
                50, max(0, self.facts.total_number - (self.page - 1) * 50)
            )
            if (
                max(1, self.facts.total_page) != expected_pages
                or self.facts.seen != expected_seen
            ):
                raise ValueError("incomplete scene count")
        elif self.page != 1 or not self.last:
            raise ValueError("unpaged resource")
        return self

    @property
    def request_id(self) -> str | None:
        return self.evidence.request_id


class ScenesGateway(Protocol):
    def read_page(
        self,
        *,
        resource: SceneResource,
        advertiser_id: str,
        page: int,
        minis_id: str | None,
    ) -> ScenePage: ...
