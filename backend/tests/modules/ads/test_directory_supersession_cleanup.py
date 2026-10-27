"""清理与运行时使用相同覆盖范围，不抢有效租约。"""

from datetime import UTC, datetime, timedelta

import pytest

from app.modules.reporting.job_tasks import _supersede_stalled_directory_runs
from tests.modules.ads.test_directory_sync import _authority, _run


@pytest.mark.parametrize(
    "covered,leased,expected",
    [(False, False, "QUEUED"), (True, False, "STALE"), (True, True, "QUEUED")],
)
def test_cleanup_requires_full_scope_coverage_and_expired_lease(
    session, directory_seed, covered, leased, expected
):
    _authority(session, directory_seed)
    old = _run(session, directory_seed, kind="ad", ad_type="SMART_PLUS")
    new = _run(session, directory_seed, kind="ad", ad_type="SMART_PLUS")
    now = datetime.now(UTC)
    old.created_at = now - timedelta(days=2)
    old.claimed_until = now + timedelta(minutes=2) if leased else None
    old.query = old.query | {"ids": ["old-ad"], "include_deleted": True}
    new.query = new.query | {
        "ids": ["old-ad"] if covered else ["different-ad"],
        "include_deleted": covered,
    }
    new.status, new.published_version = "COMPLETE", 100
    session.flush()
    session.connection().execute(
        _supersede_stalled_directory_runs(now, candidate_ids=[old.id])
    )
    session.refresh(old)
    assert old.status == expected
