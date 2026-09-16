"""真实 PostgreSQL 下大目录去重与分页保留可用副本；不调用平台。"""

from datetime import UTC, datetime
from time import perf_counter
from uuid import uuid4

from app.models import User
from app.modules.materials.models import AccountMaterial, MaterialFile
from app.modules.materials.router import get_materials
from tests.modules.materials.test_tenant_materials import mapping, material


def test_large_duplicate_catalogue_pages_keep_all_usable_representatives(
    session, context
):
    template = material(session, context, "Load-0000-a.mp4", state="unavailable")
    files = [template]
    for index in range(1, 1000):
        files.append(
            MaterialFile(
                **(
                    template.model_dump()
                    | {
                        "id": uuid4(),
                        "object_key": str(uuid4()),
                        "file_name": f"Load-{index // 2:04d}-{'b' if index % 2 else 'a'}.mp4",
                    }
                )
            )
        )
    for index, file in enumerate(files):
        file.sha256, file.video_md5 = f"{index // 2:064x}", f"{index // 2:032x}"
        file.digest_verified_at = datetime.now(UTC)
    session.add_all(files)
    session.flush()
    source = mapping(session, context, files[1])
    session.add_all(
        [
            AccountMaterial(
                **(
                    source.model_dump()
                    | {
                        "id": uuid4(),
                        "material_id": file.id,
                        "video_id": f"video-{file.id}",
                    }
                )
            )
            for file in files[3::2]
        ]
    )
    session.flush()
    expected = [file.id for file in files[1::2]]
    seen, cursor = [], None
    user = session.get(User, context.actor_id)
    started = perf_counter()
    for _ in range(5):
        page = get_materials(
            context.tenant_id, session, user, query="Load-", limit=100, cursor=cursor
        )
        assert page.total == 500
        seen.extend(row.material_id for row in page.items)
        assert all(row.status == "available" for row in page.items)
        cursor = page.next_cursor
    assert cursor is None
    assert seen == expected
    print(f"1000 records / 500 contents / 5 pages: {perf_counter() - started:.3f}s")  # noqa: T201
