"""已暂存末页的本地发布不应等待HTTP成本收据行锁。"""

from contextlib import contextmanager
from types import SimpleNamespace

from sqlmodel import select

from app.integrations.tiktok.contracts.ads import CallEvidence, DirectoryPage
from app.modules.ads import tasks
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.sync_control.models import SyncCostLedger
from tests.modules.ads.test_directory_quantum import committed_background  # noqa: F401
from tests.modules.reporting.sync_conftest import committed_sync_seed  # noqa: F401


def test_local_publication_completes_while_http_ledger_is_locked(
    committed_background,  # noqa: F811
    monkeypatch,
):
    _seed, factory, args = committed_background
    reads = []

    def read(query):
        reads.append(query.page)
        return DirectoryPage(
            items=(),
            materials=(),
            page=query.page,
            complete=True,
            next_page=None,
            evidence=CallEvidence(request_id="local-terminal"),
        )

    @contextmanager
    def gateway(**_kwargs):
        yield SimpleNamespace(ads=SimpleNamespace(read_primary_page=read))

    monkeypatch.setattr(tasks, "open_tiktok_gateway", gateway)
    assert tasks.run_directory_step(**args) == "CONTINUE"
    with factory() as blocker:
        ledger = blocker.exec(
            select(SyncCostLedger)
            .where(SyncCostLedger.directory_run_id == args["run_id"])
            .with_for_update()
        ).one()
        before = ledger.model_dump()
        assert tasks.run_directory_step(**args, publish_only=True) == "READY"
        blocker.refresh(ledger)
        assert ledger.model_dump() == before
    assert reads == [1]
    with factory() as db:
        assert db.get(AdDirectoryRun, args["run_id"]).status == "COMPLETE"
