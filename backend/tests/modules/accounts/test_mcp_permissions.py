"""可见工具与成功读取均不得扩大当前候选的写权限。"""

from sqlmodel import Session, select

from app.core.db import engine
from app.modules.accounts.connection_models import ConnectionAuthorization
from app.modules.accounts.connections import bind_candidate_bc
from app.modules.accounts.models import BCAccountAccess, DiscoveryRun
from tests.modules.accounts.test_mcp_authorization import accept, issue
from tests.modules.accounts.test_mcp_directory_publish import (  # noqa: F401
    app_config,
    bc_page,
    enqueue_directory,
    finish,
    read_bcs,
)
from tests.modules.accounts.test_mcp_directory_publish import (
    catalog_wire as _catalog_wire,
)
from tests.modules.accounts.test_mcp_directory_publish import (
    committed_context as _committed_context,
)
from tests.modules.accounts.test_mcp_directory_publish import (
    directory_context as _directory_context,
)
from tests.modules.accounts.test_mcp_directory_publish import (
    oauth_wire as _oauth_wire,
)

catalog_wire = _catalog_wire
committed_context = _committed_context
directory_context = _directory_context
oauth_wire = _oauth_wire


def test_unknown_oauth_scope_remains_unknown_after_complete_reads(
    directory_context, oauth_wire, catalog_wire, redis_client
):
    oauth_wire.data = {
        key: value for key, value in oauth_wire.data.items() if key != "scope"
    }
    state, candidate = issue(directory_context)
    assert accept(state) == candidate
    bc_id = f"bc-{directory_context.tenant_id}"
    bc_page(catalog_wire, bcs=(bc_id,), total_number=1)
    read_bcs(directory_context, candidate, redis_client)
    with Session(engine) as own:
        run_id = bind_candidate_bc(
            own, context=directory_context, attempt_id=candidate, bc_id=bc_id
        )
        own.commit()
    enqueue_directory(catalog_wire, bc_id, (f"ad-{directory_context.tenant_id}",))
    assert finish(directory_context, run_id, redis_client)[0] == "COMPLETE"
    with Session(engine) as own:
        run = own.get(DiscoveryRun, run_id)
        authorization = own.exec(
            select(ConnectionAuthorization).where(
                ConnectionAuthorization.connection_id == run.connection_id
            )
        ).one()
        assert authorization.scopes == []
        assert authorization.permission_summary == {
            "read_authorized": None,
            "upload_authorized": None,
            "build_authorized": None,
        }
        grant = own.exec(
            select(BCAccountAccess).where(
                BCAccountAccess.connection_id == run.connection_id
            )
        ).one()
        assert grant.active and grant.authorized
        assert not grant.can_build and not grant.can_upload
        assert grant.permission_state == "UNKNOWN"


def test_mcp_capability_worker_uses_frozen_connection_and_keeps_write_unknown(
    directory_context, oauth_wire, catalog_wire, redis_client, monkeypatch
):
    from uuid import uuid4

    from app.modules.accounts import capabilities
    from tests.modules.accounts.capabilities.test_service import run, start
    from tests.modules.accounts.test_mcp_directory_publish import remote_page, response

    state, candidate = issue(directory_context)
    assert accept(state) == candidate
    bc_id = f"bc-{directory_context.tenant_id}"
    advertiser_id = f"ad-{directory_context.tenant_id}"
    bc_page(catalog_wire, bcs=(bc_id,), total_number=1)
    read_bcs(directory_context, candidate, redis_client)
    with Session(engine) as own:
        run_id = bind_candidate_bc(
            own, context=directory_context, attempt_id=candidate, bc_id=bc_id
        )
        own.commit()
    enqueue_directory(catalog_wire, bc_id, (advertiser_id,))
    assert finish(directory_context, run_id, redis_client)[0] == "COMPLETE"
    with Session(engine) as own:
        connection_id = own.get(DiscoveryRun, run_id).connection_id
    monkeypatch.setattr(capabilities, "_require_bounded_worker", lambda: None)
    env = {
        "context": directory_context,
        "connection_id": connection_id,
        "bc_id": bc_id,
        "request_id": uuid4(),
    }
    job_id = start(env)
    response(
        catalog_wire,
        "bc_asset_get",
        remote_page(
            [
                {
                    "asset_id": advertiser_id,
                    "asset_type": "ADVERTISER",
                    "advertiser_role": "ADMIN",
                }
            ]
        ),
    )
    assert run(env, redis_client, job_id).phase == "PUBLISH"
    job = run(env, redis_client, job_id)
    assert job.status == "COMPLETE" and job.channel == "OFFICIAL_MCP"
    assert job.authorization_revision == 1
    assert not job.scope_known and not job.scope_build and not job.scope_upload
    with Session(engine) as own:
        grant = own.exec(
            select(BCAccountAccess).where(
                BCAccountAccess.connection_id == connection_id
            )
        ).one()
        assert not grant.can_build and not grant.can_upload
        assert grant.permission_state == "UNKNOWN"
    assert len(oauth_wire.calls) == 1


def test_directory_application_capacity_preserves_100k_and_rejects_2001_pages():
    import pytest

    from app.integrations.tiktok.contracts.accounts import (
        AccountRoleFact,
        DirectoryPage,
        require_page,
    )
    from app.integrations.tiktok.contracts.common import CallEvidence
    from app.integrations.tiktok.contracts.discovery import (
        AUTHORIZED_LIST_SOURCE,
        AuthorizedAdvertisers,
    )

    page = DirectoryPage(
        items=tuple(AccountRoleFact(f"ad-{i}", "ADMIN") for i in range(50)),
        page=2000,
        page_size=50,
        total_pages=2000,
        total_number=100000,
        last=True,
        evidence=CallEvidence(),
    )
    assert page.last
    with pytest.raises(ValueError):
        require_page(2001, 50)
    with pytest.raises(ValueError):
        AuthorizedAdvertisers(
            advertiser_ids=tuple(f"ad-{i}" for i in range(100001)),
            evidence=CallEvidence(),
            completeness_source=AUTHORIZED_LIST_SOURCE,
        )
