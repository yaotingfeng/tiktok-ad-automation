"""同一批次的快速MID查询共享会话，仍保留每任务四次读取上限。"""

import pytest

from tests.modules.materials.test_cover_throughput import image, matrix, page
from tests.modules.materials.test_covers import job_state, run
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire


@pytest.mark.parametrize("targets", [4, 5])
def test_complete_mid_probes_continue_across_targets_with_four_read_cap(
    source_env, redis_client, wire, targets
):
    identities = matrix(source_env, 1, targets)
    wire[1].extend([{"list": [image(0)]}, *[page([]) for _ in range(4)]])
    if targets == 4:
        wire[1].append({"failed_infos": {}})
    run(source_env, redis_client, identities[0])
    searches = [call for call in wire[0] if "image/ad/search" in call[1]]
    assert len(searches) == 4
    assert all("filtering" in dict(call[2]["fields"]) for call in searches)
    assert sum(call[0] == "POST" for call in wire[0]) == (1 if targets == 4 else 0)
    if targets == 5:
        assert job_state(identities[0]).status == "PENDING"
        wire[1].extend([page([]), {"failed_infos": {}}])
        run(source_env, redis_client, identities[0])
    assert job_state(identities[0]).status == "VERIFYING"
    assert sum(call[0] == "POST" for call in wire[0]) == 1
