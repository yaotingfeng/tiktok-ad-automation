"""快照失败只记录分页计数，回滚后仍可诊断且不泄露素材正文。"""

import json
import logging
from decimal import Decimal
from uuid import uuid4

import pytest

from app.modules.reporting import jobs
from app.modules.reporting.publish import publish_material_day
from app.modules.reporting.store_models import ReportSyncJob
from tests.modules.reporting.test_material_resume import (
    ACCOUNTS,
    DAY,
    Pages,
    claim,
    enqueue,
    facts,
    pages,
    row,
    run,
)


@pytest.mark.parametrize("change", ["duplicate", "grow", "shrink_empty"])
def test_snapshot_failure_logs_counts_and_preserves_published_facts(
    committed_sync_seed, monkeypatch, caplog, change
):
    monkeypatch.setattr(jobs, "MATERIAL_BACKFILL_PAGES_PER_STEP", 5)
    # 测试建库的 Alembic fileConfig 会禁用先前导入的 logger；worker 不运行此初始化。
    monkeypatch.setattr(jobs.logger, "disabled", False)
    seed, factory = committed_sync_seed
    with factory() as session:
        publish_material_day(
            session,
            tenant_id=seed.tenant_id,
            job_id=uuid4(),
            accounts=ACCOUNTS,
            start=DAY,
            end=DAY,
            rows=[row("previous-private", spend="9")],
        )
        session.commit()
    job = enqueue(factory, seed)
    reader = Pages(rows=[row(f"private-material-{i}") for i in range(7)])
    assert run(factory, job, reader) == "WAITING"
    if change == "duplicate":
        reader.rows[0], reader.rows[5] = reader.rows[5], reader.rows[0]
    elif change == "grow":
        reader.rows.append(row("new-private-material"))
    else:
        reader.rows = reader.rows[:3]
    with caplog.at_level(logging.WARNING, logger=jobs.__name__):
        assert run(factory, claim(factory, job.id), reader) == "WAITING"
    with factory() as session:
        saved = session.get(ReportSyncJob, job.id)
        assert saved.error_code == "report_snapshot_changed" and saved.attempts == 1
        assert "material_paging" not in saved.progress
    records = [r for r in caplog.records if hasattr(r, "snapshot_failure")]
    assert len(records) == 1
    details = records[0].snapshot_failure
    assert details == {
        "job_id": str(job.id),
        "reason": "cross_page_duplicate" if change == "duplicate" else "total_changed",
        "page": 6,
        "saved_total_page": 7,
        "saved_total_number": 7,
        "current_total_page": len(reader.rows),
        "current_total_number": len(reader.rows),
        "first_page_age_seconds": details["first_page_age_seconds"],
    }
    # 首页来自数据库时钟；本地 Docker 与宿主时钟偏差时不能伪报负年龄。
    age = details["first_page_age_seconds"]
    assert age is None or age >= 0
    assert "private-material" not in records[0].getMessage()
    assert "private-material" not in json.dumps(records[0].__dict__, default=str)
    assert pages(factory, job.id) == []
    assert [(f.main_material_id, f.spend) for f in facts(factory, seed.tenant_id)] == [
        ("mprevious-private", Decimal("9"))
    ]
