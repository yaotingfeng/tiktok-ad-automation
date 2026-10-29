"""轮播音乐只读合同；曲库查询成功不证明任何投放资格。"""

from dataclasses import dataclass, field
from typing import Protocol

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.common import CallEvidence
from app.integrations.tiktok.contracts.materials import RemoteCallBudget


@dataclass(frozen=True)
class MusicItem:
    music_id: str
    display_name: str
    author: str | None
    # 零时长是平台事实，不在读取层实施选曲策略。
    duration: float | None
    preview_url: str | None = field(repr=False)


@dataclass(frozen=True)
class MusicPage:
    items: tuple[MusicItem, ...]
    next_cursor: str | None = field(repr=False)
    evidence: CallEvidence
    # 推荐只读首批候选；有更多候选时不伪造可供 search 使用的游标。
    truncated: bool = False


class MusicOperations(Protocol):
    def search(
        self,
        *,
        advertiser_id: str,
        keyword: str,
        cursor: str | None,
        budget: RemoteCallBudget,
    ) -> MusicPage: ...

    def recommend(
        self,
        *,
        advertiser_id: str,
        image_urls: tuple[str, ...],
        budget: RemoteCallBudget,
    ) -> MusicPage: ...

    def read(
        self,
        *,
        advertiser_id: str,
        music_ids: tuple[str, ...],
        budget: RemoteCallBudget,
    ) -> MusicPage: ...


class UnsupportedMusicOperations:
    """退役通道保留显式能力错误，不创建客户端或回退到其他连接。"""

    def search(
        self,
        *,
        advertiser_id: str,
        keyword: str,
        cursor: str | None,
        budget: RemoteCallBudget,
    ) -> MusicPage:
        raise DomainError("music_channel_unsupported", "当前连接不支持音乐读取")

    def recommend(
        self,
        *,
        advertiser_id: str,
        image_urls: tuple[str, ...],
        budget: RemoteCallBudget,
    ) -> MusicPage:
        raise DomainError("music_channel_unsupported", "当前连接不支持音乐读取")

    def read(
        self,
        *,
        advertiser_id: str,
        music_ids: tuple[str, ...],
        budget: RemoteCallBudget,
    ) -> MusicPage:
        raise DomainError("music_channel_unsupported", "当前连接不支持音乐读取")
