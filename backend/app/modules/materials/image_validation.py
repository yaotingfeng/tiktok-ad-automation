"""只解码验证原图，不转码、不缩放，也不把动画静默截为首帧。"""

import warnings
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from app.core.errors import DomainError


@dataclass(frozen=True)
class ImageMetadata:
    format: str
    width: int
    height: int
    byte_size: int


def inspect_image(path: Path, *, max_pixels: int) -> ImageMetadata:
    if max_pixels <= 0:
        raise ValueError("max_pixels must be positive")
    try:
        # Pillow 自身的炸弹检测也必须失败关闭；不修改进程全局解码选项。
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                if image.format not in {"JPEG", "PNG"}:
                    raise DomainError("unsupported_image", "仅支持静态 JPG、PNG 图片")
                if getattr(image, "n_frames", 1) != 1 or getattr(
                    image, "is_animated", False
                ):
                    raise DomainError("unsupported_image", "暂不支持动画或多帧图片")
                width, height = image.size
                if width <= 0 or height <= 0 or width * height > max_pixels:
                    raise DomainError("image_pixel_limit", "图片像素超过本服务运行上限")
                image_format = image.format
                image.verify()
            # verify 检查文件结构；重新打开并完整 load 才能识别截断的像素数据。
            with Image.open(path) as image:
                image.load()
            return ImageMetadata(image_format, width, height, path.stat().st_size)
    except Image.DecompressionBombError, Image.DecompressionBombWarning:
        raise DomainError("image_pixel_limit", "图片像素超过本服务运行上限") from None
    except UnidentifiedImageError, OSError, SyntaxError, ValueError:
        raise DomainError("invalid_image", "图片内容损坏或无法解码") from None
