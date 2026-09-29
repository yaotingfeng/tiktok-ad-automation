"""只读外部 HTTPS 原件；固定公网 IP，TLS 按原主机校验，不跟随重定向。"""

import hashlib
import ipaddress
import logging
import socket
import ssl
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from time import monotonic
from urllib.parse import SplitResult, urlsplit

import urllib3

from app.core.config import settings
from app.core.errors import DomainError

from .object_validation import inspect_video


@dataclass(frozen=True)
class ExternalFacts:
    size: int
    mime_type: str
    sha256: str
    md5: str
    width: int
    height: int
    duration: float
    etag: str | None


def validate_url(url: str, allowed_hosts: set[str]) -> SplitResult:
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        if (
            parsed.scheme != "https"
            or not hostname
            or hostname != hostname.lower()
            or hostname not in allowed_hosts
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or parsed.port not in (None, 443)
            or not parsed.path
            or "\\" in url
            or any(ord(c) < 33 or ord(c) == 127 for c in url)
        ):
            raise ValueError
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            if any(
                not part
                or not all(c.isascii() and (c.isalnum() or c == "-") for c in part)
                for part in hostname.split(".")
            ):
                raise ValueError from None
        else:
            if not address.is_global:
                raise ValueError
    except ValueError, TypeError:
        raise DomainError(
            "push_url_invalid", "素材地址必须来自已配置的 HTTPS 来源主机"
        ) from None
    return parsed


def public_address(hostname: str) -> str:
    try:
        addresses = [
            str(result[4][0])
            for result in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        ]
        if not addresses or any(
            not ipaddress.ip_address(ip).is_global for ip in addresses
        ):
            raise ValueError
        return addresses[0]
    except OSError, ValueError:
        raise DomainError(
            "push_url_unreachable", "素材主机无法解析为允许的公网地址"
        ) from None


def inspect_external(
    url: str, allowed_hosts: set[str], file_name: str
) -> ExternalFacts:
    parsed = validate_url(url, allowed_hosts)
    assert parsed.hostname
    address = public_address(parsed.hostname)
    deadline = monotonic() + settings.MATERIAL_VALIDATION_SECONDS
    maximum = settings.MATERIAL_URL_MAX_UPLOAD_BYTES
    # 原始路径可能包含临时签名；禁止连接池 DEBUG 输出请求行。
    logging.getLogger("urllib3.connectionpool").disabled = True
    pool = urllib3.HTTPSConnectionPool(
        address,
        port=443,
        server_hostname=parsed.hostname,
        assert_hostname=parsed.hostname,
        cert_reqs=ssl.CERT_REQUIRED,
        maxsize=1,
        block=True,
    )
    response = None
    try:
        target = parsed.path + ("?" + parsed.query if parsed.query else "")
        response = pool.urlopen(
            "GET",
            target,
            headers={"Host": parsed.hostname, "Accept-Encoding": "identity"},
            preload_content=False,
            redirect=False,
            retries=False,
            timeout=urllib3.Timeout(
                connect=5, read=15, total=settings.MATERIAL_VALIDATION_SECONDS
            ),
        )
        if (
            response.status != 200
            or response.headers.get("Content-Encoding", "identity").lower()
            != "identity"
        ):
            raise DomainError("push_url_unreachable", "素材链接不可读取或返回了重定向")
        content_length = response.headers.get("Content-Length")
        expected = int(content_length) if content_length is not None else None
        if expected is not None and not 0 < expected <= maximum:
            raise DomainError("push_file_too_large", "素材大小为空或超过上传上限")
        sha, md5, length = hashlib.sha256(), hashlib.md5(usedforsecurity=False), 0
        with TemporaryDirectory(prefix="material-push-") as temporary:
            path = Path(temporary) / "original"
            with path.open("wb") as output:
                while True:
                    if monotonic() >= deadline:
                        raise DomainError("material_deadline", "素材读取超过时限")
                    chunk = response.read(
                        min(1024 * 1024, maximum - length + 1), decode_content=False
                    )
                    if not chunk:
                        break
                    length += len(chunk)
                    if length > maximum:
                        raise DomainError("push_file_too_large", "素材超过上传上限")
                    sha.update(chunk)
                    md5.update(chunk)
                    output.write(chunk)
            if length <= 0 or (expected is not None and length != expected):
                raise DomainError("push_file_incomplete", "素材未完整读取")
            media = inspect_video(path, remaining=deadline - monotonic())
        extension = PurePosixPath(file_name).suffix.lower()
        # MIME 依据扩展名分类，但必须有实际容器探测佐证，不信任 HTTP 声明。
        formats = set(str(media["format_name"]).split(","))
        expected_formats = {
            ".mp4": {"mp4"},
            ".m4v": {"mp4"},
            ".mov": {"mov"},
            ".avi": {"avi"},
            ".webm": {"webm"},
            ".mpeg": {"mpeg"},
            ".3gp": {"3gp"},
            ".mkv": {"matroska"},
        }
        if not formats.intersection(expected_formats[extension]):
            raise DomainError("invalid_video", "视频容器与文件扩展名不匹配")
        mime = {
            ".mp4": "video/mp4",
            ".m4v": "video/mp4",
            ".mov": "video/quicktime",
            ".avi": "video/x-msvideo",
            ".webm": "video/webm",
            ".mpeg": "video/mpeg",
            ".3gp": "video/3gpp",
            ".mkv": "video/x-matroska",
        }[extension]
        etag = response.headers.get("ETag")
        return ExternalFacts(
            length,
            mime,
            sha.hexdigest(),
            md5.hexdigest(),
            media["width"],
            media["height"],
            media["duration"],
            etag[:512] if etag else None,
        )
    except DomainError:
        raise
    except Exception:
        # 网络异常可包含完整 URL，向上层只传播固定错误码。
        raise DomainError(
            "push_url_unreachable", "素材读取失败，请检查链接有效期后重推"
        ) from None
    finally:
        if response is not None:
            response.close()
            response.release_conn()
        pool.close()
