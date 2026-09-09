"""Page-size limits do not impose a 50k-account product ceiling."""

import pytest

from tests.modules.accounts.capabilities.test_service import page, run, start


def test_100k_role_directory_first_page_is_persisted_without_premature_failure(
    capability_env, wire, redis_client
):
    identity = start(capability_env)
    wire[1].append(
        page(["actual-account"] + [f"capacity-{i}" for i in range(49)], 1, 100000)
    )
    result = run(capability_env, redis_client, identity)
    assert result.status == "PENDING" and result.error_code is None
    assert result.total_pages == 2000 and result.total_count == 100000
    assert (
        result.next_page == 2
        and result.seen_count == 50
        and result.published_count == 0
    )
    assert len(wire[0]) == 1


@pytest.mark.parametrize("count", [2**31, 2**63, -1])
def test_role_counts_outside_database_integer_range_fail_closed(
    capability_env, wire, redis_client, count
):
    identity = start(capability_env)
    wire[1].append(page([f"capacity-{i}" for i in range(50)], 1, count))
    result = run(capability_env, redis_client, identity)
    assert (
        result.status == "FAILED"
        and result.error_code == "capability_response_unverified"
    )
    assert result.seen_count == 0 and result.published_count == 0
