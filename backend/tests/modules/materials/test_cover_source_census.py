"""源图片未知上传只读恢复，跨页重叠不能永久截断后续候选。"""

import pytest

from tests.modules.materials.test_covers import image_info, job_state, run, unknown
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire


@pytest.mark.parametrize("complete", [True, False])
def test_source_unknown_census_moves_page_boundary_without_reupload(
    source_env, redis_client, wire, complete
):
    identity = unknown(source_env, redis_client, wire)

    def inventory(indices, number, size):
        rows = [
            image_info(identity)["list"][0]
            if index == 156
            else {"image_id": f"other-{index}", "file_name": "unrelated.jpg"}
            for index in indices
        ]
        return {
            "list": rows,
            "page_info": {
                "page": number,
                "page_size": size,
                "total_number": 156,
                "total_page": (156 + size - 1) // size,
            },
        }

    wire[1].extend(
        [inventory(range(1, 101), 1, 100), inventory(range(98, 154), 2, 100)]
    )
    run(source_env, redis_client, identity, read=True)
    run(source_env, redis_client, identity, read=True)
    assert job_state(identity).status == "VERIFYING"
    assert job_state(identity).next_page == 3
    wire[1].extend(
        [
            inventory(range(1, 72), 1, 71),
            inventory(range(86, 157) if complete else range(83, 154), 2, 71),
        ]
    )
    run(source_env, redis_client, identity, read=True)
    run(source_env, redis_client, identity, read=True)
    if complete:
        assert job_state(identity).known_image_id == "target-image"
        wire[1].append(image_info(identity))
        run(source_env, redis_client, identity, read=True)
        assert job_state(identity).status == "READY"
    else:
        wire[1].append(inventory(range(140, 154), 3, 71))
        run(source_env, redis_client, identity, read=True)
        wire[1].extend(
            [
                inventory(range(1, 54), 1, 53),
                inventory(range(54, 107), 2, 53),
                inventory(range(104, 154), 3, 53),
            ]
        )
        for _ in range(3):
            run(source_env, redis_client, identity, read=True)
        assert job_state(identity).status == "UNKNOWN"
        assert job_state(identity).error_code == "cover_search_incomplete"
        assert job_state(identity).dispatch_id is None
    assert sum(call[0] == "POST" for call in wire[0]) == 1
