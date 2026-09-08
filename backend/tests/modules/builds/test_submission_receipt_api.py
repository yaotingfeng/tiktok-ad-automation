from uuid import uuid4

from tests.modules.builds.test_previews import prepared as prepared
from tests.modules.builds.test_submissions import frozen as frozen
from tests.modules.strategies.test_api import headers


def test_submit_response_recovered_by_same_request_without_another_write(
    client, context, other_context, frozen
):
    request_id = uuid4()
    base = f"/api/tenants/{context.tenant_id}"
    route = f"{base}/submission-requests/{request_id}"
    assert client.get(route, headers=headers(context)).status_code == 404
    first = client.post(
        f"{base}/build-previews/{frozen}/submit",
        json={"request_id": str(request_id)},
        headers=headers(context),
    )
    assert first.status_code == 202
    restored = client.get(route, headers=headers(context))
    assert restored.status_code == 200 and restored.json() == first.json()
    replay = client.post(
        f"{base}/build-previews/{frozen}/submit",
        json={"request_id": str(request_id)},
        headers=headers(context),
    )
    assert replay.json() == first.json()
    assert (
        client.get(
            f"/api/tenants/{other_context.tenant_id}/submission-requests/{request_id}",
            headers=headers(other_context),
        ).status_code
        == 404
    )
