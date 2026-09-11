"""只读目录合同；未知授权不等于拒绝，也不构成写入许可。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from .common import CallEvidence


def require_id(value: str) -> None:
    if type(value) is not str or not value.strip() or len(value) > 255:
        raise ValueError("invalid exact identifier")


def require_page(page: int, page_size: int) -> None:
    # 本地工程容量：50条/页下保留已支持的100k角色目录；并非官方服务上限。
    if (
        type(page) is not int
        or not 1 <= page <= 2000
        or type(page_size) is not int
        or not 1 <= page_size <= 100
    ):
        raise ValueError("invalid directory pagination")


@dataclass(frozen=True)
class AuthorizationFacts:
    subject_id: str | None
    grant_id: str | None
    issuer: str
    resource: str
    scopes: tuple[str, ...]
    read_authorized: bool | None
    upload_authorized: bool | None
    build_authorized: bool | None
    evidence_source: str
    observed_at: datetime

    def __post_init__(self) -> None:
        for value in (self.issuer, self.resource, self.evidence_source):
            require_id(value)
        for optional_identity in (self.subject_id, self.grant_id):
            if optional_identity is not None:
                require_id(optional_identity)
        if type(self.scopes) is not tuple or len(set(self.scopes)) != len(self.scopes):
            raise ValueError("invalid scopes")
        for scope in self.scopes:
            require_id(scope)
        for flag in (
            self.read_authorized,
            self.upload_authorized,
            self.build_authorized,
        ):
            if flag is not None and type(flag) is not bool:
                raise ValueError("invalid authorization flag")
        if (
            not isinstance(self.observed_at, datetime)
            or self.observed_at.tzinfo is None
            or self.observed_at.utcoffset() is None
        ):
            raise ValueError("authorization observation needs timezone")


@dataclass(frozen=True)
class BusinessCenterFact:
    bc_id: str
    name: str

    def __post_init__(self) -> None:
        require_id(self.bc_id)
        if type(self.name) is not str:
            raise ValueError("invalid name")


@dataclass(frozen=True)
class AdvertiserFact:
    advertiser_id: str
    name: str
    currency: str
    timezone: str
    remote_status: str
    authorized: bool | None

    def __post_init__(self) -> None:
        require_id(self.advertiser_id)
        if any(
            type(value) is not str
            for value in (self.name, self.currency, self.timezone, self.remote_status)
        ):
            raise ValueError("invalid advertiser details")
        if self.authorized is not None and type(self.authorized) is not bool:
            raise ValueError("invalid authorization flag")


@dataclass(frozen=True)
class AccountRoleFact:
    advertiser_id: str
    role: Literal["ADMIN", "OPERATOR", "ANALYST"] | None

    def __post_init__(self) -> None:
        require_id(self.advertiser_id)
        if self.role not in (None, "ADMIN", "OPERATOR", "ANALYST"):
            raise ValueError("unrecognized account role")


@dataclass(frozen=True)
class DirectoryPage[T]:
    items: tuple[T, ...]
    page: int
    page_size: int
    total_pages: int
    total_number: int | None
    last: bool
    evidence: CallEvidence

    def __post_init__(self) -> None:
        require_page(self.page, self.page_size)
        if type(self.total_pages) is not int or not 0 <= self.total_pages <= 2000:
            raise ValueError("invalid total pages")
        if type(self.items) is not tuple or len(self.items) > self.page_size:
            raise ValueError("invalid directory items")
        if (
            self.page > max(1, self.total_pages)
            or type(self.last) is not bool
            or self.last != (self.page == max(1, self.total_pages))
        ):
            raise ValueError("inconsistent last page")
        if (self.total_pages == 0 and self.items) or (not self.last and not self.items):
            raise ValueError("incomplete directory page")
        if self.total_number is not None:
            if (
                type(self.total_number) is not int
                or self.total_number < len(self.items)
                or self.total_number < 0
            ):
                raise ValueError("invalid total number")
            if self.total_pages == 0 and self.total_number != 0:
                raise ValueError("inconsistent empty directory")
            expected_pages = max(
                1, (self.total_number + self.page_size - 1) // self.page_size
            )
            if max(1, self.total_pages) != expected_pages:
                raise ValueError("inconsistent directory totals")
            expected_rows = min(
                self.page_size,
                max(0, self.total_number - (self.page - 1) * self.page_size),
            )
            if len(self.items) != expected_rows:
                raise ValueError("incomplete directory count")
        ids = []
        for item in self.items:
            if isinstance(item, BusinessCenterFact):
                ids.append(item.bc_id)
            elif isinstance(item, (AdvertiserFact, AccountRoleFact)):
                ids.append(item.advertiser_id)
            else:
                raise ValueError("untyped directory fact")
        if len(ids) != len(set(ids)) or len({type(item) for item in self.items}) > 1:
            raise ValueError("duplicate or mixed directory facts")
        if not isinstance(self.evidence, CallEvidence):
            raise ValueError("invalid evidence")

    @property
    def request_id(self) -> str | None:
        return self.evidence.request_id


@dataclass(frozen=True)
class RuntimeReadContext:
    bc_id: str

    def __post_init__(self) -> None:
        require_id(self.bc_id)


@dataclass(frozen=True)
class CandidateReadContext:
    """仅内部候选目录工厂创建；业务参数无法开启该模式。"""


class AccountsGateway(Protocol):
    def authorization_facts(self) -> AuthorizationFacts: ...
    def business_centers(
        self, *, page: int, page_size: int
    ) -> DirectoryPage[BusinessCenterFact]: ...
    def advertisers(
        self, *, bc_id: str, page: int, page_size: int
    ) -> DirectoryPage[AdvertiserFact]: ...
    def roles(
        self, *, bc_id: str, page: int, page_size: int
    ) -> DirectoryPage[AccountRoleFact]: ...
