"""整批广告结构独立覆盖：不修改策略，不重新匹配或编排图片。"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_serializer
from sqlmodel import Session

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.modules.strategies.schemas import StrategyConfig


class StructureOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")
    group_generation_mode: Literal["FIXED", "BY_MATERIAL"] | None = None
    group_count: int | None = Field(default=None, gt=0, le=100, strict=True)
    group_material_allocation: Literal["SHARED", "SEQUENTIAL_AVERAGE"] | None = None
    max_materials_per_group: int | None = Field(default=None, gt=0, strict=True)
    ad_generation_mode: Literal["FIXED", "BY_MATERIAL"] | None = None
    ads_per_group: int | None = Field(default=None, gt=0, le=100, strict=True)
    ad_material_allocation: Literal["SHARED", "SEQUENTIAL_AVERAGE"] | None = None
    max_materials_per_ad: int | None = Field(default=None, gt=0, strict=True)
    creative_count: int | None = Field(default=None, gt=0, strict=True)

    @model_serializer(mode="wrap")
    def serialize_patch(self, handler):
        # 公共读取必须保持字段稀疏，否则未覆盖字段会被错误转换为显式 null。
        return {
            key: value
            for key, value in handler(self).items()
            if key in self.model_fields_set
        }


class ImageStructureChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    expected_revision: int = Field(ge=1, strict=True)
    structure_override: StructureOverride


def effective_structure_config(strategy_version, draft_override) -> StrategyConfig:
    """P3 规划器直接复用；显式 null 与未覆盖区分，组合后统一校验策略结构。"""
    source = (
        strategy_version
        if isinstance(strategy_version, StrategyConfig)
        else strategy_version.config
    )
    base = (
        source.model_dump(mode="json") if isinstance(source, StrategyConfig) else source
    )
    patch = StructureOverride.model_validate(draft_override or {})
    return StrategyConfig.model_validate(
        {**base, **patch.model_dump(exclude_unset=True)}
    )


def save_image_structure(
    session: Session, *, context: TenantContext, draft_id: UUID, body: ImageStructureChange
) -> int:
    from datetime import UTC, datetime

    from sqlmodel import select

    from app.modules.strategies.service import get_version

    from .image_composition_service import (
        _receipt,
        _request,
        assert_no_image_run,
        resolve_image_scope,
    )
    from .image_composition_state import ImageCandidateSnapshot, ImageCompositionState

    scope = resolve_image_scope(session, context=context, draft_id=draft_id)
    value = body.structure_override.model_dump(mode="json", exclude_unset=True)
    draft, fingerprint, replay = _request(
        session, scope, body.request_id, "structure", body.expected_revision, value
    )
    if replay is not None:
        return replay["revision"]
    assert_no_image_run(session, draft)
    try:
        effective_structure_config(
            get_version(session, context=context, version_id=draft.strategy_version_id),
            value,
        )
    except ValidationError:
        raise DomainError(
            "image_edit_invalid", "本次广告结构无效，请核对生成方式和数量"
        ) from None
    if (draft.structure_override or {}) != value:
        was_applied = draft.applied_image_rule_revision == draft.revision
        draft.structure_override = value
        # 结构不参与图片匹配，不触发通用修改的候选取消；只移动未采纳快照的 CAS 基准。
        state = session.get(ImageCompositionState, (context.tenant_id, draft_id))
        snapshots = session.exec(
            select(ImageCandidateSnapshot).where(
                ImageCandidateSnapshot.tenant_id == context.tenant_id,
                ImageCandidateSnapshot.draft_id == draft_id,
                ImageCandidateSnapshot.base_revision == draft.revision,
                ImageCandidateSnapshot.id != state.snapshot_id
                if state and state.snapshot_id
                else True,
            )
        ).all()
        draft.revision += 1
        draft.updated_at = datetime.now(UTC)
        for snapshot in snapshots:
            snapshot.base_revision = draft.revision
        session.add(draft)
        session.flush()
        if was_applied:
            draft.applied_image_rule_revision = draft.revision
    return _receipt(
        session, scope, body.request_id, fingerprint, {"revision": draft.revision}
    )["revision"]
