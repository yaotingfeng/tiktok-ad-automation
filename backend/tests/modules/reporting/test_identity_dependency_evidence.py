"""身份范围变化必须唤醒等待任务；普通目录刷新不能反复唤醒。"""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from app.modules.ads.models import AdObject
from app.modules.reporting.link_work import wake_link_dependencies
from app.modules.reporting.material_links import wait_for_dependencies
from tests.modules.reporting.test_spark_content_identity import prepare


@pytest.mark.parametrize("change", ["arrives", "identity", "metadata", "order"])
def test_identity_evidence_controls_dependency_wakeup(session, sync_seed, change):
    job, _ = prepare(session, sync_seed)
    ad = session.get(AdObject, (sync_seed.tenant_id, "acc-1", "ad", "ad"))
    complete = deepcopy(ad.configuration)
    if change == "arrives":
        ad.configuration = {}
    session.flush()
    now = datetime.now(UTC)
    job.status = "WAITING"
    assert wait_for_dependencies(session, job, now=now)
    due = job.due_at
    config = deepcopy(complete)
    if change == "identity":
        for row in config["creative_list"]:
            row["creative_info"]["identity_id"] = "new-identity"
    elif change == "metadata":
        config["creative_list"][0]["creative_info"]["video_info"]["name"] = "renamed"
    elif change == "order":
        config["creative_list"].reverse()
    ad.configuration = config
    session.flush()
    changed = change in {"arrives", "identity"}
    assert wake_link_dependencies(session, now=now + timedelta(seconds=1)) == int(
        changed
    )
    if changed:
        assert job.due_at == now + timedelta(seconds=1)
        assert job.error_code is None
    else:
        assert job.due_at == due
        assert job.error_code == "material_identity_unavailable"
