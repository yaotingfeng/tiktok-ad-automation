"""轮播目录的说明性身份：保留图片顺序和曲目，不代表图片级经济事实。"""

from dataclasses import dataclass
from typing import Any

from .accounts import require_id


@dataclass(frozen=True)
class CarouselCreativeIdentity:
    advertiser_id: str
    ad_id: str
    remote_creative_id: str | None
    ordered_image_ids: tuple[str, ...]
    music_id: str

    def __post_init__(self):
        for value in (self.advertiser_id, self.ad_id, self.music_id):
            require_id(value)
        if self.remote_creative_id is not None:
            require_id(self.remote_creative_id)
        if (
            type(self.ordered_image_ids) is not tuple
            or not 1 <= len(self.ordered_image_ids) <= 35
        ):
            raise ValueError("invalid ordered carousel members")
        for image in self.ordered_image_ids:
            require_id(image)


@dataclass(frozen=True)
class CarouselDirectory:
    identities: tuple[CarouselCreativeIdentity, ...] = ()
    is_carousel: bool = False
    complete: bool = True


def decode_carousels(
    row: dict[str, Any],
    *,
    advertiser_id: str,
    ad_id: str,
    smart: bool,
    creative_id: str | None = None,
) -> CarouselDirectory:
    """缺少内容证据只标未关联；错误容器或冲突身份拒绝整页，禁止猜名称/默认曲目。"""
    entries = row.get("creative_list", []) if smart else [row]
    if type(entries) is not list:
        raise ValueError("invalid creative list")
    identities = []
    found = row.get("ad_format") == "CAROUSEL_ADS"
    complete = not (found and not entries)
    for entry in entries:
        if type(entry) is not dict:
            raise ValueError("invalid creative entry")
        info = entry.get("creative_info", {}) if smart else entry
        if type(info) is not dict:
            raise ValueError("invalid creative info")
        # 顶层已明确轮播时，条目缺字段不能被当成其他格式跳过。
        carousel = (
            row.get("ad_format") == "CAROUSEL_ADS"
            or info.get("ad_format") == "CAROUSEL_ADS"
        )
        images = info.get("image_info") if smart else info.get("image_ids")
        # 视频封面与 Spark 帖子不是轮播主素材；不能污染原视频/帖子身份命名空间。
        has_video = (
            info.get("video_info") or info.get("video_id") or info.get("tiktok_item_id")
        )
        if has_video:
            if carousel:
                raise ValueError("conflicting carousel media")
            continue
        if not carousel and not images:
            continue
        found = True
        if images is None:
            complete = False
            continue
        if type(images) is not list:
            raise ValueError("invalid image members")
        ordered = []
        for image in images:
            if type(image) is dict:
                image_id, web_uri = image.get("image_id"), image.get("web_uri")
                if image_id is not None and web_uri is not None and image_id != web_uri:
                    raise ValueError("conflicting image identity")
                image = image_id if image_id is not None else web_uri
            if image is None:
                complete = False
                break
            require_id(image)
            ordered.append(image)
        music = info.get("music_info") if smart else None
        if music is not None and type(music) is not dict:
            raise ValueError("invalid music info")
        song = music.get("music_id") if music else info.get("music_id")
        native = entry.get("smart_plus_creative_id") if smart else creative_id
        if native is not None:
            require_id(native)
        if song is None or len(ordered) != len(images) or not ordered:
            complete = False
            continue
        identities.append(
            CarouselCreativeIdentity(advertiser_id, ad_id, native, tuple(ordered), song)
        )
    return CarouselDirectory(tuple(identities), found, complete)
