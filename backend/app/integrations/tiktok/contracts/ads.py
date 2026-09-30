"""广告目录只读合同；身份精确保留，授权由绑定上下文另行校验。"""

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol
from uuid import UUID

from .accounts import require_id
from .common import CallEvidence

EntityKind = Literal["campaign", "adgroup", "ad", "creative"]
ENTITY_KINDS = ("campaign", "adgroup", "ad", "creative")


def require_text(value: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError("invalid nonempty text")


def require_strings(values: tuple[str, ...], *, nonempty: bool = False) -> None:
    if type(values) is not tuple or (nonempty and not values):
        raise ValueError("invalid string tuple")
    for value in values:
        require_id(value)
    if len(set(values)) != len(values):
        raise ValueError("duplicate identifiers")


def require_aware(value: datetime) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("timestamp needs timezone")


def require_positive_int(value: int) -> None:
    if type(value) is not int or value < 1:
        raise ValueError("invalid positive integer")


def require_page_state(
    page: int, next_page: int | None, complete: bool, evidence: CallEvidence
) -> None:
    require_positive_int(page)
    if next_page is not None:
        require_positive_int(next_page)
        if next_page <= page:
            raise ValueError("continuation must advance current page")
    # 无后续页不等于成功完整；解析失败等情况允许 False + None。
    if type(complete) is not bool or (complete and next_page is not None):
        raise ValueError("inconsistent page completion")
    if not isinstance(evidence, CallEvidence):
        raise ValueError("invalid call evidence")


def require_optional_status(value: str | None) -> None:
    if value is not None:
        require_text(value)


@dataclass(frozen=True)
class EntityRef:
    tenant_id: UUID
    advertiser_id: str
    kind: EntityKind
    remote_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.tenant_id, UUID):
            raise ValueError("invalid tenant identity")
        require_id(self.advertiser_id)
        require_id(self.remote_id)
        if self.kind not in ENTITY_KINDS:
            raise ValueError("invalid entity kind")


@dataclass(frozen=True)
class MaterialUseRef:
    ad_ref: EntityRef
    platform_material_id: str
    ad_material_id: str | None
    material_type: str

    def __post_init__(self) -> None:
        if not isinstance(self.ad_ref, EntityRef) or self.ad_ref.kind != "ad":
            raise ValueError("material usage requires ad identity")
        require_id(self.platform_material_id)
        # 普通广告可没有广告内素材 ID；不能用平台 VID 补齐独立启停身份。
        if self.ad_material_id is not None:
            require_id(self.ad_material_id)
        require_text(self.material_type)


@dataclass(frozen=True)
class AdEntity:
    ref: EntityRef
    parent_ref: EntityRef | None
    ad_type: str
    name: str
    configuration: dict[str, Any]
    operation_status: str | None
    review_status: str | None
    delivery_status: str | None
    observed_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.ref, EntityRef):
            raise ValueError("invalid ad entity identity")
        if self.parent_ref is not None and (
            not isinstance(self.parent_ref, EntityRef)
            or self.parent_ref.tenant_id != self.ref.tenant_id
            or self.parent_ref.advertiser_id != self.ref.advertiser_id
        ):
            raise ValueError("parent must share tenant and advertiser")
        require_text(self.ad_type)
        if type(self.name) is not str or type(self.configuration) is not dict:
            raise ValueError("invalid ad entity details")
        for status in (self.operation_status, self.review_status, self.delivery_status):
            require_optional_status(status)
        require_aware(self.observed_at)
        # frozen 禁止字段重绑；复制容器隔离传输方后续修改，保留约定 dict 类型。
        object.__setattr__(self, "configuration", deepcopy(self.configuration))


@dataclass(frozen=True)
class AdMaterialUsage:
    use_ref: MaterialUseRef
    local_material_id: UUID | None
    operation_status: str | None
    complete: bool

    def __post_init__(self) -> None:
        if not isinstance(self.use_ref, MaterialUseRef):
            raise ValueError("invalid material usage identity")
        if self.local_material_id is not None and not isinstance(
            self.local_material_id, UUID
        ):
            raise ValueError("invalid local material identity")
        require_optional_status(self.operation_status)
        if type(self.complete) is not bool:
            raise ValueError("invalid material completeness")


@dataclass(frozen=True)
class DirectoryQuery:
    advertiser_id: str
    kind: str
    ad_type: str
    page: int
    page_size: int
    ids: tuple[str, ...]
    parent_ids: tuple[str, ...]
    include_deleted: bool

    def __post_init__(self) -> None:
        require_id(self.advertiser_id)
        if self.kind not in ENTITY_KINDS:
            raise ValueError("invalid directory entity kind")
        require_text(self.ad_type)
        require_positive_int(self.page)
        require_positive_int(self.page_size)
        require_strings(self.ids)
        require_strings(self.parent_ids)
        if type(self.include_deleted) is not bool:
            raise ValueError("invalid deleted filter")


@dataclass(frozen=True)
class DirectoryPage:
    items: tuple[AdEntity, ...]
    materials: tuple[AdMaterialUsage, ...]
    next_page: int | None
    complete: bool
    evidence: CallEvidence
    page: int = 1

    def __post_init__(self) -> None:
        if type(self.items) is not tuple or any(
            not isinstance(item, AdEntity) for item in self.items
        ):
            raise ValueError("invalid ad directory items")
        if type(self.materials) is not tuple or any(
            not isinstance(item, AdMaterialUsage) for item in self.materials
        ):
            raise ValueError("invalid material usage items")
        require_page_state(self.page, self.next_page, self.complete, self.evidence)


class AdsReadOperations(Protocol):
    def read_page(self, query: DirectoryQuery) -> DirectoryPage: ...
