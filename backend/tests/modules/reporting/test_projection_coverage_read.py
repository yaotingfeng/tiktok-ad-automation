"""全局投影覆盖只读取窄列，输出与原模型路径逐字段相同。"""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import event, text

from app.modules.reporting.read_models import coverage
from app.modules.reporting.read_models.models import ReadBucketState


@pytest.mark.parametrize("timezone", ["UTC", "Asia/Hong_Kong"])
@pytest.mark.parametrize("mode", ["empty", "current", "dirty", "missing"])
def test_projection_coverage_raw_read_matches_models(
    session, sync_seed, timezone, mode
):
    day = date(2026, 10, 1)
    session.connection().execute(
        text("SELECT set_config('TimeZone',:zone,true)"), {"zone": timezone}
    )
    if mode != "empty":
        revisions = (
            [(3, 3), (4, 4)]
            if mode == "current"
            else [(3, 2), (4, 4)]
            if mode == "dirty"
            else [(3, 0), (4, 2)]
        )
        for adv, (source, derived) in zip(("acc-1", "acc-2"), revisions, strict=True):
            session.add(
                ReadBucketState(
                    tenant_id=sync_seed.tenant_id,
                    advertiser_id=adv,
                    kind="material_day",
                    bucket=day,
                    source_revision=source,
                    derived_revision=derived,
                    derived_status="COMPLETE",
                    derived_observed_at=datetime(
                        2026, 10, 1, 3, 4, 5, 123456, tzinfo=UTC
                    )
                    if derived
                    else None,
                )
            )
        session.flush()
        session.expire_all()
    kwargs = {
        "tenant_id": sync_seed.tenant_id,
        "advertisers": ["acc-2", "acc-1"],
        "start": day,
        "end": day,
    }
    _, models = coverage.projected_states(session, **kwargs)
    expected = coverage.projection_coverage(models)
    assert coverage.read_projection_coverage(session, **kwargs) == expected
    if expected["metrics_observed_at"]:
        assert expected["metrics_observed_at"].endswith(
            "+00:00" if timezone == "UTC" else "+08:00"
        )


def test_projection_coverage_does_not_construct_sync_or_bucket_models(
    session, sync_seed, monkeypatch
):
    day = date(2026, 10, 1)
    session.add(
        ReadBucketState(
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-1",
            kind="material_day",
            bucket=day,
            source_revision=1,
            derived_revision=1,
        )
    )
    session.flush()
    session.expunge_all()
    loaded = []

    def reject_sync(**_kwargs):
        raise AssertionError("global coverage must not instantiate ReportSyncState")

    def track_bucket(target, _context):
        loaded.append(target)

    monkeypatch.setattr(coverage, "ReportSyncState", reject_sync)
    event.listen(ReadBucketState, "load", track_bucket)
    try:
        result = coverage.read_projection_coverage(
            session,
            tenant_id=sync_seed.tenant_id,
            advertisers=["acc-1"],
            start=day,
            end=day,
        )
    finally:
        event.remove(ReadBucketState, "load", track_bucket)
    assert result["status"] == "CURRENT"
    assert loaded == []


def test_projection_coverage_empty_scope_uses_same_empty_digest(session, sync_seed):
    result = coverage.read_projection_coverage(
        session,
        tenant_id=sync_seed.tenant_id,
        advertisers=[],
        start=date(2026, 10, 1),
        end=date(2026, 10, 7),
    )
    assert result == coverage.projection_coverage([])
