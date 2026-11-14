from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.core.errors import DomainError
from app.models import User
from app.modules.tenants.models import TenantMembership


def admin(session, context, public=False):
    member = session.get(TenantMembership, (context.tenant_id, context.actor_id))
    member.role = "tenant_admin"
    if public:
        session.get(User, context.actor_id).is_superuser = True
    session.flush()


def test_versioned_knowledge_freezes_content_and_scope(session, context):
    from app.modules.ai_workbench.knowledge import (
        KnowledgeInput,
        create_version,
        select_knowledge,
    )

    admin(session, context, public=True)
    from app.modules.accounts.models import TenantBC

    session.add(TenantBC(tenant_id=context.tenant_id, bc_id="bc-a"))
    session.flush()
    source = KnowledgeInput(
        key="window",
        scope="PUBLIC",
        title="完整日",
        body="完整日不代表归因成熟。",
        source="platform-methods",
        tags=["scale"],
    )
    public = create_version(session, context=context, body=source)
    private = create_version(
        session,
        context=context,
        body=source.model_copy(
            update={
                "key": "case",
                "scope": "BC",
                "bc_id": "bc-a",
                "body": "私有案例",
                "tags": ["scale"],
            }
        ),
    )
    frozen = select_knowledge(session, context=context, bc_id="bc-a", tags={"scale"})
    assert {k["id"] for k in frozen} == {str(public.id), str(private.id)}
    assert all(k["body"] and k["sha256"] for k in frozen)
    assert str(private.id) not in {
        k["id"]
        for k in select_knowledge(
            session, context=context, bc_id="bc-b", tags={"scale"}
        )
    }
    other = replace(context, tenant_id=uuid4())
    assert {
        k["id"]
        for k in select_knowledge(session, context=other, bc_id="bc-a", tags={"scale"})
    } == {str(public.id)}
    updated = create_version(
        session,
        context=context,
        body=source.model_copy(update={"body": "新版正文"}),
        asset_id=public.asset_id,
    )
    assert updated.version == 2
    assert (
        next(k for k in frozen if k["id"] == str(public.id))["body"]
        == "完整日不代表归因成熟。"
    )
    assert str(updated.id) in {
        k["id"]
        for k in select_knowledge(
            session, context=context, bc_id="bc-a", tags={"scale"}
        )
    }


def test_private_promotion_requires_explicit_public_review(session, context):
    from app.modules.ai_workbench.knowledge import (
        KnowledgeInput,
        create_version,
        promote_public,
    )

    admin(session, context)
    private = create_version(
        session,
        context=context,
        body=KnowledgeInput(key="case", title="案例", body="原始案例", source="manual"),
    )
    with pytest.raises(DomainError):
        create_version(
            session,
            context=context,
            body=KnowledgeInput(
                key="public", scope="PUBLIC", title="公开", body="正文", source="manual"
            ),
        )
    with pytest.raises(DomainError):
        promote_public(
            session,
            context=context,
            version_id=private.id,
            body="提炼方法",
            review="已审核可共享",
        )
    admin(session, context, public=True)
    public = promote_public(
        session,
        context=context,
        version_id=private.id,
        body="脱敏后的通用方法",
        review="已移除身份、账户、费率和私有经营事实，确认公开共享",
    )
    assert public.body != private.body
    assert public.tenant_id is None and public.bc_id is None
    assert str(private.id) not in public.source


def test_expired_and_conflicting_knowledge_are_marked(session, context):
    from app.modules.ai_workbench.knowledge import (
        KnowledgeInput,
        create_version,
        select_knowledge,
    )

    admin(session, context)
    for key in ("one", "two"):
        create_version(
            session,
            context=context,
            body=KnowledgeInput(
                key=key,
                title=key,
                body="待核实内容",
                source="manual",
                conflict_key="roas",
                expires_at=datetime.now(UTC) - timedelta(days=1),
            ),
        )
    selected = select_knowledge(session, context=context, bc_id="bc-a", tags=set())
    assert len(selected) == 2
    assert all(k["eligibility"] == "EXPIRED" and k["conflict"] for k in selected)
