"""素材证据契约及真实双通道适配器；外部替身只在 HTTP 边界。"""

from dataclasses import replace

from app.integrations.tiktok.contracts.common import CallEvidence
from app.integrations.tiktok.contracts.materials import VideoRecord
from app.modules.materials.sdk_assets import video_identity


def test_video_identity_never_uses_requested_digest_as_remote_evidence():
    row = VideoRecord(
        "a",
        "v",
        "m",
        None,
        "fixed.mp4",
        1080,
        1920,
        120,
        4.5,
        "mp4",
        True,
        CallEvidence(request_id="req"),
    )
    assert (
        video_identity(
            row, advertiser_id="a", video_id="v", md5="a" * 32, expected_size=120
        )
        is None
    )
    verified = replace(row, md5="a" * 32)
    assert (
        video_identity(
            verified, advertiser_id="b", video_id="v", md5="a" * 32, expected_size=120
        )
        is None
    )
    assert video_identity(
        verified, advertiser_id="a", video_id="v", md5="a" * 32, expected_size=120
    ) == {"video_id": "v", "mid": "m"}
