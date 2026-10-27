"""冻结素材上下文独立暂存，调度query保持小，元数据不冒充平台成功页。"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import select

from app.core.config import settings
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.ads import CallEvidence, DirectoryPage
from app.modules.ads.material_directory_children import material_child_scope
from app.modules.ads.sync import publish_directory, stage_directory_page
from app.modules.ads.sync_models import AdDirectoryPage
from app.modules.reporting.job_tasks import cleanup_directory_pages
from tests.modules.ads.test_directory_sync import _authority, _run
from tests.modules.ads.test_material_child_dispatch import _child

CONTEXT_PAGE = 2_147_483_647


@pytest.fixture
def child_case(session, directory_seed, monkeypatch):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    _authority(session, directory_seed)
    return _child(session, directory_seed, age=datetime.now(UTC) - timedelta(hours=1))


def test_child_context_is_own_non_provider_page_and_query_stays_small(
    session, child_case
):
    source, child = child_case
    marker = child.query["material_child_v1"]
    assert marker == {"source_run_id": str(source.id), "context_page": CONTEXT_PAGE}
    assert len(json.dumps(child.query)) < 1000
    context = session.get(AdDirectoryPage, (child.id, CONTEXT_PAGE))
    assert context is not None
    assert context.coverage == "MATERIAL_CONTEXT"
    assert context.items == context.materials == []
    assert not context.complete and context.next_page is None
    assert context.published_version is None
    assert material_child_scope(session, child) is not None


@pytest.mark.parametrize("damage", ["missing", "pointer", "scope", "source"])
def test_invalid_child_context_fails_without_reconstructing_from_source(
    session, child_case, damage
):
    _, child = child_case
    context = session.get(AdDirectoryPage, (child.id, CONTEXT_PAGE))
    assert context is not None
    if damage == "missing":
        session.delete(context)
    elif damage == "pointer":
        child.query = child.query | {
            "material_child_v1": child.query["material_child_v1"] | {"context_page": 1}
        }
    else:
        state = dict(context.evidence["material_child_v1"])
        state["scope_signature" if damage == "scope" else "source_run_id"] = "invalid"
        context.evidence = {"material_child_v1": state}
    session.flush()
    with pytest.raises(DomainError, match="冻结范围"):
        material_child_scope(session, child)


def test_child_provider_cannot_write_or_link_to_context_page(session, child_case):
    _, child = child_case
    for page in (
        DirectoryPage((), (), None, True, CallEvidence(), page=CONTEXT_PAGE),
        DirectoryPage((), (), CONTEXT_PAGE, False, CallEvidence()),
    ):
        with pytest.raises(DomainError, match="页码"):
            stage_directory_page(
                session, run_id=child.id, claim_generation=1, page=page
            )


def test_normal_run_cannot_ignore_metadata_shaped_orphan_page(session, directory_seed):
    _authority(session, directory_seed)
    run = _run(session, directory_seed)
    stage_directory_page(
        session,
        run_id=run.id,
        claim_generation=1,
        page=DirectoryPage((), (), None, True, CallEvidence()),
    )
    session.add(
        AdDirectoryPage(
            run_id=run.id,
            page=CONTEXT_PAGE,
            tenant_id=run.tenant_id,
            advertiser_id=run.advertiser_id,
            claim_generation=1,
            complete=False,
            coverage="MATERIAL_CONTEXT",
            items=[],
            materials=[],
            evidence={},
        )
    )
    session.flush()
    with pytest.raises(DomainError, match="断开的页"):
        publish_directory(session, run_id=run.id, claim_generation=1)


def test_context_kept_while_active_and_cleaned_with_finished_child(session, child_case):
    _, child = child_case
    future = datetime.now(UTC) + timedelta(hours=2)
    assert cleanup_directory_pages(session, now=future, candidate_ids=[child.id]) == 0
    stage_directory_page(
        session,
        run_id=child.id,
        claim_generation=1,
        page=DirectoryPage((), (), None, True, CallEvidence()),
    )
    publish_directory(session, run_id=child.id, claim_generation=1)
    context = session.get(AdDirectoryPage, (child.id, CONTEXT_PAGE))
    assert (
        context is not None
        and not context.complete
        and context.published_version is None
    )
    assert cleanup_directory_pages(session, now=future, candidate_ids=[child.id]) == 2
    assert not session.exec(
        select(AdDirectoryPage).where(AdDirectoryPage.run_id == child.id)
    ).all()
