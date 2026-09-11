from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import MetaData, Table, text
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session, select

from app.modules.builds.models import BuildDraft
from app.modules.builds.preview_models import BuildPreview, PreviewDrama
from app.modules.providers.models import PromotionLink, ProviderDrama
from tests.migration_database import historical_database
from tests.modules.builds.test_drafts import create_intent
from tests.modules.builds.test_naming_numbers import preview_row
from tests.modules.conftest import create_context


def test_migration_preserves_frozen_preview_and_its_immutable_snapshot(monkeypatch):
    with historical_database(monkeypatch, "r2_part_receipts") as (engine, alembic):
        with Session(engine) as session:
            context = create_context(session)
            intent = create_intent(session, context)
            # 迁移前只能播种当时列；当前 create_draft 会查询后续新增的连接偏好。
            draft = BuildDraft(
                tenant_id=context.tenant_id,
                **{
                    key: value
                    for key, value in intent.items()
                    if key not in {"drama_lines", "account_lines"}
                },
                created_by=context.actor_id,
                request_id=uuid4(),
                request_digest="d" * 64,
            )
            table = Table("build_draft", MetaData(), autoload_with=session.connection())
            assert "execution_connection_id" not in table.c
            session.execute(
                table.insert().values(
                    **{
                        key: value
                        for key, value in draft.model_dump().items()
                        if key in table.c
                    }
                )
            )
            row = preview_row(context, intent, draft.id)
            row.batch_short_id = uuid4().hex
            row.config.pop("campaign_name_template")
            row.content_digest = "a" * 64
            session.add(row)
            session.flush()
            drama = ProviderDrama(
                tenant_id=context.tenant_id,
                connection_id=intent["provider_connection_id"],
                application_id=intent["application_id"],
                external_drama_id="101",
                title="Historic title",
            )
            session.add(drama)
            session.flush()
            link = PromotionLink(
                tenant_id=context.tenant_id,
                connection_id=intent["provider_connection_id"],
                application_id=intent["application_id"],
                drama_id=drama.id,
                reuse_key="historic-link",
                config={},
            )
            session.add(link)
            session.flush()
            session.execute(
                text("""
                INSERT INTO preview_drama (tenant_id, preview_id, bc_id, drama_id, link_id, title, url, protected_base, reason_codes)
                VALUES (:tenant, :preview, :bc, :drama, :link, 'Historic title', 'https://example.com', 'protected-historic', '[]'::jsonb)
            """),
                {
                    "tenant": context.tenant_id,
                    "preview": row.id,
                    "bc": row.bc_id,
                    "drama": drama.id,
                    "link": link.id,
                },
            )
            row.status = "FROZEN"
            session.add(row)
            session.commit()
            identity, original = row.id, row.model_dump(mode="json")
        # 只核验命名迁移；后续通道迁移对缺历史路由的预览另有明确失效规则。
        command.upgrade(alembic, "0017_preview_naming")
        with Session(engine) as session:
            assert (
                session.get(BuildPreview, identity).model_dump(mode="json") == original
            )
            frozen = session.exec(
                select(PreviewDrama).where(PreviewDrama.preview_id == identity)
            ).one()
            assert (
                frozen.title == "Historic title"
                and frozen.protected_base == "protected-historic"
            )
            assert frozen.provider_pinyin == frozen.external_drama_id == ""
            with pytest.raises(DBAPIError), session.begin_nested():
                session.execute(
                    text(
                        "UPDATE preview_drama SET provider_pinyin='jiashu' WHERE preview_id=:id"
                    ),
                    {"id": identity},
                )
