"""Dedicated-PG capacity harness guard and actual route/fixture lifecycle."""

from tests.acceptance.test_r2_probe import load_script


def test_capacity_refuses_main_database_before_loading_runtime():
    import pytest

    script = load_script("acceptance-r2-capacity.py")
    with pytest.raises(ValueError, match="dedicated PostgreSQL test database"):
        script.run_capacity(
            count=201, database_url="postgresql://localhost/application"
        )


def test_capacity_uses_real_routes_and_removes_only_its_fixture():
    from sqlmodel import Session, select

    from app.core.db import engine
    from app.modules.tenants.models import Tenant

    script = load_script("acceptance-r2-capacity.py")
    with Session(engine) as db:
        before = set(db.exec(select(Tenant.id)).all())
    report = script.run_capacity(count=201)
    assert report["mode"] == "synthetic_fastapi_jwt_postgresql"
    assert report["files"] == 201 and report["chunks"] == 2
    assert report["pages"] == 3 and report["unique_files"] == 201
    assert report["summary"]["sql_max"] <= 20
    assert report["backpressure"]["waiting_observed"] is True
    assert report["backpressure"]["resumed_after_evidence"] is True
    assert (
        report["backpressure"]["reserved_peak_bytes"]
        <= report["backpressure"]["tenant_limit_bytes"]
    )
    assert report["cleanup"]["fixture_removed"] is True
    assert report["mixed_faults_1000"]["status"] == "pending"
    with Session(engine) as db:
        assert set(db.exec(select(Tenant.id)).all()) == before


def test_capacity_failure_cleans_own_fixture_and_budget_delta(monkeypatch):
    import pytest
    from sqlmodel import Session, select

    from app.core.db import engine
    from app.modules.materials.ingest_models import ObjectBudget
    from app.modules.tenants.models import Tenant

    script = load_script("acceptance-r2-capacity.py")
    with Session(engine) as db:
        tenants = set(db.exec(select(Tenant.id)).all())
        budget = db.get(ObjectBudget, "global")
        reserved = budget.reserved_bytes if budget else 0

    def fail(_self, **_values):
        raise RuntimeError("synthetic fault")

    monkeypatch.setattr(script.MetadataStorage, "create_multipart_upload", fail)
    with pytest.raises(RuntimeError):
        script.run_capacity(count=1)
    with Session(engine) as db:
        assert set(db.exec(select(Tenant.id)).all()) == tenants
        assert db.get(ObjectBudget, "global").reserved_bytes == reserved
