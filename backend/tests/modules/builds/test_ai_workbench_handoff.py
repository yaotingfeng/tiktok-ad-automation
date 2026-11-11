from uuid import uuid4

import pytest

from app.core.errors import DomainError
from app.modules.ai_workbench.build_tasks import dispatch_occurrence_task


def test_occurrence_worker_rejects_extra_payload_fields() -> None:
    with pytest.raises(DomainError) as caught:
        dispatch_occurrence_task(
            database_engine=object(),
            tenant_id=uuid4(),
            actor_id=uuid4(),
            payload={"occurrence_id": str(uuid4()), "strategy_version_id": str(uuid4())},
        )
    assert caught.value.code == "dispatch_payload_invalid"


def test_occurrence_worker_rejects_invalid_occurrence_id() -> None:
    with pytest.raises(DomainError) as caught:
        dispatch_occurrence_task(
            database_engine=object(),
            tenant_id=uuid4(),
            actor_id=uuid4(),
            payload={"occurrence_id": "not-a-uuid"},
        )
    assert caught.value.code == "dispatch_payload_invalid"
