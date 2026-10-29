"""租户预设库：追加不可变快照；加载复用草稿事务、候选授权与配乐任务。"""

from datetime import UTC
from uuid import UUID, uuid4

from sqlalchemy import func, text
from sqlmodel import col, select

from app.core.errors import DomainError
from app.modules.builds import image_composition_service as composition
from app.modules.builds.image_composition import (
    music_rule_digest,
    pending_music_status,
    resolve_image_rule,
)
from app.modules.builds.image_composition_models import (
    DraftCreativeUnit,
    DraftCreativeUnitItem,
)
from app.modules.builds.image_composition_schemas import CreativeUnit, ImageRule
from app.modules.materials.carousel_models import (
    CarouselPresetItem,
    CarouselPresetVersion,
)
from app.modules.materials.models import MaterialFile
from app.modules.materials.object_uses import retain_preset_original
from app.modules.tenants.permissions import require_tenant


def _public(version):
    result = version.model_dump(
        mode="json", exclude={"actor_id", "copy_request_id", "copy_request_digest"}
    )
    result["created_at"] = version.created_at.astimezone(UTC).isoformat()
    return result


def get_preset_version(session, context, version_id):
    return _public(_version(session, context, version_id))


def _version(session, context, version_id):
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    row = session.exec(
        select(CarouselPresetVersion).where(
            CarouselPresetVersion.tenant_id == context.tenant_id,
            CarouselPresetVersion.id == UUID(str(version_id)),
        )
    ).first()
    if row is None:
        raise DomainError("carousel_preset_not_found", "轮播版本不存在")
    return row


def list_preset_versions(session, context, *, name="", offset=0, limit=50):
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    if not 0 <= offset or not 1 <= limit <= 100 or len(name) > 200:
        raise DomainError("carousel_preset_invalid", "轮播查询参数无效")
    query = select(CarouselPresetVersion).where(
        CarouselPresetVersion.tenant_id == context.tenant_id
    )
    if name:
        query = query.where(
            col(CarouselPresetVersion.name).icontains(name, autoescape=True)
        )
    total = session.exec(select(func.count()).select_from(query.subquery())).one()
    rows = session.exec(
        query.order_by(
            CarouselPresetVersion.name, col(CarouselPresetVersion.version).desc()
        )
        .offset(offset)
        .limit(limit)
    ).all()
    return {
        "items": [_public(r) for r in rows],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


def _selected_snapshot(unit):
    # 只拷贝实际已选曲目。pending_request、预览 URL、推荐参数和 job 标识不可复用。
    if unit.music_status != "READY" or not unit.music_id:
        return (
            None,
            {},
            "NEEDS_SELECTION" if unit.music_status == "NEEDS_SELECTION" else "PENDING",
        )
    allowed = {"display_name", "author", "duration", "selection_source"}
    snapshot = {k: v for k, v in unit.music_snapshot.items() if k in allowed}
    snapshot["music_id"] = unit.music_id
    return unit.music_id, snapshot, "READY"


def _append_version(session, context, *, name, facts, members):
    name = name.strip()
    if not name or len(name) > 200:
        raise DomainError("carousel_preset_invalid", "轮播名称须为 1–200 个字符")
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"carousel-preset:{context.tenant_id}:{name}"},
    )
    number = (
        session.exec(
            select(func.coalesce(func.max(CarouselPresetVersion.version), 0)).where(
                CarouselPresetVersion.tenant_id == context.tenant_id,
                CarouselPresetVersion.name == name,
            )
        ).one()
        + 1
    )
    # 先按固定顺序锁素材，再插入成员 FK。反过来会让两个不同名称版本先各持有
    # KEY SHARE，再同时升级 FOR UPDATE，产生真实 PostgreSQL 死锁。
    locked = session.exec(
        select(MaterialFile)
        .where(
            MaterialFile.tenant_id == context.tenant_id,
            col(MaterialFile.id).in_([identity for identity, _ in members]),
            MaterialFile.media_type == "IMAGE",
        )
        .order_by(col(MaterialFile.id))
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    content = {file.id: file.sha256 for file in locked}
    if any(content.get(identity) != sha for identity, sha in members):
        raise DomainError("image_candidate_unavailable", "预设图片内容已变化")
    row = CarouselPresetVersion(
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        name=name,
        version=number,
        **facts,
    )
    session.add(row)
    session.flush()
    session.add_all(
        [
            CarouselPresetItem(
                tenant_id=context.tenant_id,
                version_id=row.id,
                position=index,
                material_id=identity,
                content_digest=sha,
            )
            for index, (identity, sha) in enumerate(members, 1)
        ]
    )
    session.flush()
    # 固定锁序避免不同顺序的相同组合并发保存时发生对象锁死锁。
    for identity, _ in sorted(members, key=lambda item: str(item[0])):
        retain_preset_original(
            session, context=context, material_id=identity, version_id=row.id
        )
    return _public(row)


def save_preset_version(
    session, scope, source_unit_id, expected_revision, name, request_id
):
    draft, fp, replay = composition._request(
        session,
        scope,
        request_id,
        "save-carousel-preset",
        expected_revision,
        {"source_unit_id": source_unit_id, "name": name},
    )
    if replay is not None:
        return replay
    composition._editable(session, draft)
    source = session.get(
        DraftCreativeUnit,
        (
            scope.tenant_id,
            draft.id,
            draft.active_image_generation,
            UUID(str(source_unit_id)),
        ),
    )
    if source is None or source.drama_id not in {
        r.drama_id for r in composition._dramas(session, scope)
    }:
        raise DomainError("image_composition_not_found", "创意不属于当前草稿")
    unit = composition._unit_value(session, source)
    materials = {
        r.id: r
        for r in session.exec(
            select(MaterialFile).where(
                MaterialFile.tenant_id == scope.tenant_id,
                col(MaterialFile.id).in_(unit.ordered_material_ids),
                MaterialFile.media_type == "IMAGE",
            )
        ).all()
    }
    if set(materials) != set(unit.ordered_material_ids) or any(
        not r.sha256 for r in materials.values()
    ):
        raise DomainError("image_candidate_unavailable", "预设图片不完整")
    members = [
        (identity, materials[identity].sha256) for identity in unit.ordered_material_ids
    ]
    if (
        composition.digest([[str(i), sha] for i, sha in members])
        != source.content_digest
    ):
        raise DomainError("image_candidate_unavailable", "图片内容已变化，请刷新草稿")
    music_id, snapshot, status = _selected_snapshot(source)
    result = _append_version(
        session,
        scope.context,
        name=name,
        members=members,
        facts={
            "source_bc_id": scope.bc_id,
            "source_draft_id": draft.id,
            "source_unit_id": source.id,
            "source_drama_id": source.drama_id,
            "content_digest": source.content_digest,
            "ordered_material_ids": [str(i) for i in unit.ordered_material_ids],
            "music_id": music_id,
            "music_snapshot": snapshot,
            "music_status": status,
        },
    )
    return composition._receipt(session, scope, request_id, fp, result)


def copy_preset_version(session, context, preset_version_id, name, request_id):
    """库内复制不依赖任何草稿/BC；不可变版本自身就是幂等回执。"""
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="build"
    )
    fingerprint = composition.digest(
        {"preset_version_id": str(preset_version_id), "name": name}
    )
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"carousel-copy:{context.tenant_id}:{request_id}"},
    )
    prior = session.exec(
        select(CarouselPresetVersion).where(
            CarouselPresetVersion.tenant_id == context.tenant_id,
            CarouselPresetVersion.copy_request_id == request_id,
        )
    ).first()
    if prior:
        if prior.copy_request_digest != fingerprint:
            raise DomainError("idempotency_conflict", "请求标识已用于不同参数")
        return _public(prior)
    source = _version(session, context, preset_version_id)
    members = session.exec(
        select(CarouselPresetItem)
        .where(
            CarouselPresetItem.tenant_id == context.tenant_id,
            CarouselPresetItem.version_id == source.id,
        )
        .order_by(col(CarouselPresetItem.position))
    ).all()
    facts = source.model_dump(
        exclude={
            "id",
            "tenant_id",
            "actor_id",
            "name",
            "version",
            "created_at",
            "copy_request_id",
            "copy_request_digest",
        }
    )
    facts.update(copy_request_id=request_id, copy_request_digest=fingerprint)
    return _append_version(
        session,
        context,
        name=name,
        facts=facts,
        members=[(r.material_id, r.content_digest) for r in members],
    )


def load_preset_into_draft(
    session,
    scope,
    preset_version_id,
    request_id,
    expected_draft_revision,
    *,
    drama_id,
    allow_reuse=False,
):
    draft, fp, replay = composition._request(
        session,
        scope,
        request_id,
        "load-carousel-preset",
        expected_draft_revision,
        {
            "preset_version_id": preset_version_id,
            "drama_id": drama_id,
            "allow_reuse": allow_reuse,
        },
    )
    if replay is not None:
        return replay
    composition._editable(session, draft)
    preset = _version(session, scope.context, preset_version_id)
    drama_id = UUID(str(drama_id))
    ids = tuple(UUID(i) for i in preset.ordered_material_ids)
    state = composition._state(session, draft)
    exclusions = composition._exclusions(session, scope)
    if any(
        exclusions.get((drama_id, i)) and exclusions[(drama_id, i)].excluded
        for i in ids
    ):
        raise DomainError(
            "image_candidate_unavailable", "预设包含已排除图片，请先显式恢复候选"
        )
    # 只查询至多 35 个成员的重用情况，不逐个加载草稿中的上千个单元。
    used = set(
        session.exec(
            select(DraftCreativeUnitItem.material_id).where(
                DraftCreativeUnitItem.tenant_id == scope.tenant_id,
                DraftCreativeUnitItem.draft_id == draft.id,
                DraftCreativeUnitItem.generation == draft.active_image_generation,
                col(DraftCreativeUnitItem.material_id).in_(ids),
            )
        ).all()
    )
    last_position = session.exec(
        select(func.coalesce(func.max(DraftCreativeUnit.position), 0)).where(
            DraftCreativeUnit.tenant_id == scope.tenant_id,
            DraftCreativeUnit.draft_id == draft.id,
            DraftCreativeUnit.generation == draft.active_image_generation,
            DraftCreativeUnit.drama_id == drama_id,
        )
    ).one()
    if used.intersection(ids) and not allow_reuse:
        raise DomainError(
            "image_reuse_confirmation_required", "预设与当前创意重用图片，需要显式选择"
        )
    # 验证并加入候选只做一次事务内操作，不链式调用公共 API 或重复递增草稿版本。
    composition.include_candidate_images(
        session, scope, draft, state, drama_id, ids, reuse_existing=True
    )
    content = composition._validate_images(session, scope, state, drama_id, ids)
    if content != preset.content_digest:
        raise DomainError(
            "image_candidate_unavailable", "预设图片内容已变化，无法完整载入"
        )
    if composition._cross_conflicts(
        session, scope, composition._candidates(session, scope, state)
    ):
        raise DomainError("image_composition_conflict", "请先明确分配跨剧候选图片")
    rule = resolve_image_rule(
        ImageRule.model_validate(state.rule or {}),
        state.drama_patches.get(str(drama_id), {}),
    )
    if not draft.active_image_generation:
        draft.active_image_generation = state.next_generation
        state.next_generation += 1
    selected = preset.music_id is not None
    unit = CreativeUnit(
        id=uuid4(),
        ordered_material_ids=ids,
        content_digest=content,
        position=last_position + 1,
        composition_protected=True,
        music_protected=selected,
        music_id=preset.music_id,
        music_status="READY" if selected else pending_music_status(rule, len(ids)),
        music_rule_digest=music_rule_digest(rule, len(ids)),
        music_revision=1 if selected else 0,
        music_request_generation=1,
    )
    composition._store_unit(
        session,
        scope,
        draft.active_image_generation,
        drama_id,
        unit,
        preset.music_snapshot,
    )
    composition.finish_creative_mutation(session, scope, draft, expected_draft_revision)
    result = {
        "revision": draft.revision,
        "unit": {
            **unit.model_dump(mode="json"),
            "drama_id": str(drama_id),
            "music_snapshot": preset.music_snapshot,
        },
    }
    return composition._receipt(session, scope, request_id, fp, result)
