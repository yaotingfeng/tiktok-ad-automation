"""文件名称不代表内容；SQL 候选和分发登记使用相同可信内容键。"""

from datetime import UTC, datetime

from sqlmodel import select

from app.modules.materials.models import MaterialFile
from tests.modules.materials.test_tenant_materials import material


def test_content_key_only_deduplicates_verified_full_digest(session, context):
    from app.modules.materials.content_identity import (
        content_key,
        material_content_key_expression,
    )

    a = material(session, context, "Moon.mp4")
    b = material(session, context, "Different name.mp4", bc="bc-b")
    for row in (a, b):
        row.sha256, row.video_md5 = "a" * 64, "b" * 32
    assert content_key(a) != content_key(b)
    for row in (a, b):
        row.digest_verified_at = datetime.now(UTC)
        session.add(row)
    session.flush()
    assert content_key(a) == content_key(b)
    expression = material_content_key_expression()
    assert session.exec(
        select(expression).where(MaterialFile.id == a.id)
    ).one() == content_key(a)
    b.byte_size += 1
    assert content_key(a) != content_key(b)


def test_invalid_digest_cannot_collapse_distinct_files(session, context):
    from app.modules.materials.content_identity import (
        content_key,
        material_content_key_expression,
    )

    a = material(session, context, "Same name.mp4")
    b = material(session, context, "Same name.mp4")
    for row in (a, b):
        row.sha256, row.video_md5 = "not-sha", "not-md5"
        row.digest_verified_at = datetime.now(UTC)
        session.add(row)
    session.flush()
    assert content_key(a) != content_key(b)
    assert session.exec(
        select(material_content_key_expression()).where(MaterialFile.id == b.id)
    ).one() == content_key(b)
