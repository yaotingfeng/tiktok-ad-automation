"""单剧确定性编排：不读库、不推荐、不分配实体 UUID。"""

import hashlib
import json
import re
import unicodedata
from collections import defaultdict, deque
from typing import Any
from uuid import UUID

from app.modules.builds.image_composition_schemas import (
    CompositionConflict,
    CompositionDiff,
    CompositionMode,
    CompositionWarning,
    CreativeUnit,
    ExcludedImage,
    ImageCandidate,
    ImageRule,
    ImageRulePatch,
    MusicStatus,
    ProposedCreativeUnit,
)


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()


def resolve_image_rule(batch: ImageRule, patch: dict[str, Any]) -> ImageRule:
    explicit = ImageRulePatch.model_validate(patch).model_dump(exclude_unset=True)
    return ImageRule.model_validate(batch.model_dump() | explicit)


def _natural_key(candidate: ImageCandidate) -> tuple:
    normalized = unicodedata.normalize("NFKC", candidate.file_name_snapshot).casefold()
    # 仅 ASCII 数字参加整数比较；完整规范化名用于 02/2 的稳定平局。
    parts = tuple(
        (1, int(part)) if part.isascii() and part.isdigit() else (0, part)
        for part in re.split(r"([0-9]+)", normalized)
    )
    return parts, normalized, candidate.file_name_snapshot, str(candidate.material_id)


def _upload_key(candidate: ImageCandidate) -> tuple:
    if candidate.upload_sequence is None:
        return (
            candidate.created_at,
            str(candidate.material_id),
            -1,
            str(candidate.material_id),
        )
    return (
        candidate.upload_batch_created_at,
        str(candidate.upload_batch_id),
        candidate.upload_sequence,
        str(candidate.material_id),
    )


def effective_music_rule(rule: ImageRule, count: int) -> dict[str, Any]:
    # 无关曲目字段不使已有推荐作废；只有该单元真正生效的配乐规则入摘要。
    if rule.music_mode == "FIXED":
        return {"mode": "FIXED", "music_id": rule.fixed_music_id}
    if count == 1:
        return {"mode": "SINGLE", "music_id": rule.single_music_id}
    return {"mode": "RECOMMEND", "version": 1}


def music_rule_digest(rule: ImageRule, count: int) -> str:
    """T2/T3 共享有效规则身份，单图不会误用多图推荐摘要。"""
    return _digest(effective_music_rule(rule, count))


def pending_music_status(rule: ImageRule, count: int) -> MusicStatus:
    """新手工创意不猜原曲；已有显式候选交 T3 核验后采用。"""
    effective = effective_music_rule(rule, count)
    return (
        "PENDING"
        if effective["mode"] == "RECOMMEND" or effective.get("music_id")
        else "NEEDS_SELECTION"
    )


def compose_images(
    candidates: tuple[ImageCandidate, ...],
    rule: ImageRule,
    previous: tuple[CreativeUnit, ...],
    mode: CompositionMode = "PRESERVE_MANUAL",
) -> CompositionDiff:
    if mode not in {"PRESERVE_MANUAL", "REPLACE_ALL"}:
        raise ValueError("无效重生成模式")
    if any(unit.kind != "CAROUSEL" for unit in previous):
        raise ValueError("图片编排只接受 CAROUSEL 创意")
    if len({u.id for u in previous}) != len(previous):
        raise ValueError("已有创意 ID 不能重复")
    unique: dict[UUID, ImageCandidate] = {}
    for candidate in candidates:
        if (
            candidate.material_id in unique
            and unique[candidate.material_id] != candidate
        ):
            raise ValueError("同一素材的候选快照不一致")
        unique[candidate.material_id] = candidate
    rows = sorted(
        unique.values(),
        key=_natural_key if rule.order == "NATURAL_NAME" else _upload_key,
    )
    old = sorted(previous, key=lambda unit: (unit.position, str(unit.id)))
    protected = [
        u for u in old if mode == "PRESERVE_MANUAL" and u.composition_protected
    ]
    reserved = {mid for unit in protected for mid in unit.ordered_material_ids}
    available = [r for r in rows if r.enabled and r.material_id not in reserved]
    excluded = [
        ExcludedImage(material_id=r.material_id, reason="DISABLED")
        for r in rows
        if not r.enabled
    ]
    conflicts: list[CompositionConflict] = []
    for unit in protected:
        missing = tuple(
            mid
            for mid in unit.ordered_material_ids
            if mid not in unique or not unique[mid].enabled
        )
        if missing:
            conflicts.append(
                CompositionConflict(
                    code="PROTECTED_IMAGE_UNAVAILABLE",
                    unit_id=unit.id,
                    material_ids=missing,
                )
            )

    # 同内容副本仍是不同实体。按旧位置逐一匹配，不能用 digest 字典覆盖副本。
    reusable: dict[tuple[UUID, ...], deque[CreativeUnit]] = defaultdict(deque)
    for unit in old:
        if unit not in protected:
            reusable[unit.ordered_material_ids].append(unit)
    groups = [
        available[i : i + rule.images_per_unit]
        for i in range(0, len(available), rule.images_per_unit)
    ]
    if (
        groups
        and len(groups[-1]) < rule.images_per_unit
        and rule.remainder == "EXCLUDE"
    ):
        excluded.extend(
            ExcludedImage(material_id=r.material_id, reason="REMAINDER")
            for r in groups.pop()
        )
    units: list[CreativeUnit | ProposedCreativeUnit] = []

    def configure_music(
        unit: CreativeUnit | ProposedCreativeUnit, prior: CreativeUnit | None
    ):
        effective = effective_music_rule(rule, len(unit.ordered_material_ids))
        digest = _digest(effective)
        if prior and mode == "PRESERVE_MANUAL" and prior.music_protected:
            return unit
        if (
            prior
            and not (mode == "REPLACE_ALL" and prior.music_protected)
            and prior.music_rule_digest == digest
            and prior.content_digest == unit.content_digest
        ):
            return unit.model_copy(update={"music_protected": False})
        music_id = effective.get("music_id")
        return unit.model_copy(
            update={
                "music_rule_digest": digest,
                "music_id": music_id,
                "music_status": "READY"
                if music_id
                else (pending_music_status(rule, len(unit.ordered_material_ids))),
                "music_protected": False,
                "music_revision": prior.music_revision + 1 if prior else 0,
                "music_request_generation": prior.music_request_generation + 1
                if prior
                else 0,
            }
        )

    for prior in protected:
        units.append(
            configure_music(prior, prior).model_copy(
                update={"position": len(units) + 1}
            )
        )
    for group in groups:
        ids = tuple(row.material_id for row in group)
        digest = _digest([(str(row.material_id), row.content_digest) for row in group])
        prior = reusable[ids].popleft() if reusable[ids] else None
        if prior:
            unit = prior.model_copy(
                update={
                    "content_digest": digest,
                    "composition_protected": False,
                    "revision": prior.revision + (prior.content_digest != digest),
                    "position": len(units) + 1,
                }
            )
        else:
            unit = ProposedCreativeUnit(
                ordered_material_ids=ids, content_digest=digest, position=len(units) + 1
            )
        units.append(configure_music(unit, prior))
    retained = tuple(unit.id for unit in units if unit.id is not None)
    retained_set = set(retained)
    for prior in old:
        if (
            mode == "PRESERVE_MANUAL"
            and prior.music_protected
            and prior.id not in retained_set
        ):
            conflicts.append(
                CompositionConflict(
                    code="PROTECTED_MUSIC_COMPOSITION_CHANGED",
                    unit_id=prior.id,
                    material_ids=prior.ordered_material_ids,
                )
            )
    if not units:
        conflicts.append(CompositionConflict(code="NO_IMAGES"))
    return CompositionDiff(
        units=tuple(units),
        retained_ids=retained,
        removed_ids=tuple(u.id for u in old if u.id not in retained_set),
        excluded_images=tuple(excluded),
        conflicts=tuple(conflicts),
        rule_digest=_digest(rule.model_dump()),
        candidate_digest=_digest(
            [
                row.model_dump(mode="json")
                for row in sorted(unique.values(), key=lambda r: str(r.material_id))
            ]
        ),
        warnings=tuple(
            CompositionWarning(
                material_id=r.material_id, reason="UPLOAD_SEQUENCE_MISSING"
            )
            for r in rows
            if rule.order == "UPLOAD_SEQUENCE" and r.upload_sequence is None
        ),
        protection_losses=tuple(
            u.id
            for u in old
            if mode == "REPLACE_ALL" and (u.composition_protected or u.music_protected)
        ),
    )
