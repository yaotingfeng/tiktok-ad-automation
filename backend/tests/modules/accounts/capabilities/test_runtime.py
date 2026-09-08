from types import SimpleNamespace

import pytest

from app.core.errors import DomainError
from app.jobs.tasks import dispatch_queue
from app.modules.accounts import capabilities
from app.modules.accounts.capability_tasks import refresh_capabilities_task


@pytest.mark.parametrize(
    "override,valid", [(None, True), (40, True), (46, False), (0, False), (True, False)]
)
def test_effective_prefork_deadline_uses_task_default_only_when_no_override(
    monkeypatch, override, valid
):
    monkeypatch.setattr(
        capabilities,
        "current_process",
        lambda: SimpleNamespace(daemon=True, name="ForkPoolWorker-1"),
    )
    monkeypatch.setattr(
        capabilities,
        "current_task",
        SimpleNamespace(
            time_limit=45,
            request=SimpleNamespace(
                called_directly=False, is_eager=False, timelimit=(override, None)
            ),
        ),
    )
    if valid:
        capabilities._require_bounded_worker()
    else:
        with pytest.raises(DomainError) as error:
            capabilities._require_bounded_worker()
        assert error.value.code == "capability_worker_unbounded"
    assert refresh_capabilities_task.time_limit == 45
    assert dispatch_queue(capabilities.TASK_NAME) == "resources"


def test_direct_calls_are_rejected_before_processing():
    with pytest.raises(DomainError) as error:
        capabilities._require_bounded_worker()
    assert error.value.code == "capability_worker_unbounded"
