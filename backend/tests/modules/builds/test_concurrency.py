from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlmodel import Session, select

from app.core.errors import DomainError
from app.modules.builds.drafts import create_draft, edit_material_groups, prepare_draft
from app.modules.builds.models import BuildDraft, DraftDrama, DraftPreparation
from tests.modules.builds.test_drafts import account, create_intent, finish, ready_links
from tests.modules.materials.test_tenant_materials import material


@pytest.mark.parametrize("operation", ["create", "prepare", "edit"])
def test_concurrent_draft_requests_have_one_ordered_result(
    isolated_strategy_database, operation
):
    engine, context, _ = isolated_strategy_database
    with Session(engine) as session:
        values = create_intent(session, context)
        # 当前准备入口要求明确的 BC 绑定与默认连接，沿共享合成授权事实播种。
        account(session, context)
        draft = create_draft(session, context=context, **values)
        prep = prepare_draft(
            session, context=context, draft_id=draft, request_id=uuid4()
        )
        ready_links(session, context, prep, values)
        finish(session, context, prep)
        drama = (
            session.exec(select(DraftDrama).where(DraftDrama.draft_id == draft))
            .first()
            .drama_id
        )
        file = material(session, context, "manual.mp4", bc="bc-draft").id
        session.commit()
    barrier = Barrier(2)
    request = uuid4()

    def run(_):
        with Session(engine) as session:
            barrier.wait(timeout=10)
            try:
                if operation == "create":
                    result = create_draft(
                        session, context=context, request_id=request, **values
                    )
                elif operation == "prepare":
                    result = prepare_draft(
                        session, context=context, draft_id=draft, request_id=request
                    )
                else:
                    result = edit_material_groups(
                        session,
                        context=context,
                        draft_id=draft,
                        drama_id=drama,
                        expected_revision=1,
                        groups=[[file]],
                    )
                session.commit()
                return result
            except DomainError as error:
                assert error.code == "draft_revision_conflict"
                return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(run, range(2)))
    with Session(engine) as session:
        if operation == "edit":
            assert results.count(None) == 1
            assert session.get(BuildDraft, draft).revision == 2
        else:
            assert results[0] == results[1] and results[0] is not None
            if operation == "prepare":
                assert session.get(DraftPreparation, results[0]).draft_revision == 2
