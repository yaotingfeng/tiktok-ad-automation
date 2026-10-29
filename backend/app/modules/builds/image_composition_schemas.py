"""图片规则与纯编排提案；实体 ID 只由持久化试算服务分配。"""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

MediaKind = Literal["VIDEO", "CAROUSEL"]
CompositionMode = Literal["PRESERVE_MANUAL", "REPLACE_ALL"]
MusicStatus = Literal[
    "NOT_REQUIRED", "NEEDS_SELECTION", "PENDING", "RUNNING", "READY", "FAILED"
]
MusicId = Annotated[str, Field(min_length=1, max_length=255)]


class ImmutableModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ImageRule(ImmutableModel):
    mode: Literal["SINGLE", "SEQUENTIAL"] = "SEQUENTIAL"
    images_per_unit: int = Field(default=3, ge=1, le=35, strict=True)
    order: Literal["NATURAL_NAME", "UPLOAD_SEQUENCE"] = "NATURAL_NAME"
    remainder: Literal["KEEP", "EXCLUDE"] = "KEEP"
    music_mode: Literal["RECOMMEND", "FIXED"] = "RECOMMEND"
    fixed_music_id: MusicId | None = None
    single_music_id: MusicId | None = None

    @model_validator(mode="after")
    def validate_count(self) -> ImageRule:
        if (self.mode == "SINGLE") != (self.images_per_unit == 1):
            raise ValueError("单图必须为 1 张，顺序轮播必须为 2–35 张")
        return self


class ImageRulePatch(ImmutableModel):
    """仅 model_dump(exclude_unset=True) 为覆盖；None 可显式清除曲目。"""

    mode: Literal["SINGLE", "SEQUENTIAL"] | None = None
    images_per_unit: int | None = Field(default=None, ge=1, le=35, strict=True)
    order: Literal["NATURAL_NAME", "UPLOAD_SEQUENCE"] | None = None
    remainder: Literal["KEEP", "EXCLUDE"] | None = None
    music_mode: Literal["RECOMMEND", "FIXED"] | None = None
    fixed_music_id: MusicId | None = None
    single_music_id: MusicId | None = None

    @model_validator(mode="after")
    def validate_explicit_null(self) -> ImageRulePatch:
        for key in self.model_fields_set - {"fixed_music_id", "single_music_id"}:
            if getattr(self, key) is None:
                raise ValueError(f"{key} 不可为空")
        return self


class ImageCandidate(ImmutableModel):
    material_id: UUID
    file_name_snapshot: str
    upload_batch_id: UUID | None = None
    upload_batch_created_at: AwareDatetime | None = None
    # P1 IngestSessionFile.client_index 的快照；旧文件可无值。
    upload_sequence: int | None = Field(default=None, ge=0, strict=True)
    created_at: AwareDatetime
    content_digest: str = Field(min_length=1)
    enabled: bool = True

    @model_validator(mode="after")
    def validate_upload_sequence(self) -> ImageCandidate:
        if self.upload_sequence is not None and (
            self.upload_batch_id is None or self.upload_batch_created_at is None
        ):
            raise ValueError("上传序号必须同时包含批次 ID 和批次创建时间")
        return self


class CreativeUnitContent(ImmutableModel):
    kind: MediaKind = "CAROUSEL"
    ordered_material_ids: tuple[UUID, ...] = Field(min_length=1, max_length=35)
    content_digest: str = Field(min_length=1)
    music_id: MusicId | None = None
    music_rule_digest: str = ""
    music_status: MusicStatus = "PENDING"
    composition_protected: bool = False
    music_protected: bool = False
    position: int = Field(ge=1, strict=True)
    revision: int = Field(default=1, ge=1, strict=True)
    music_revision: int = Field(default=0, ge=0, strict=True)
    music_request_generation: int = Field(default=0, ge=0, strict=True)

    @model_validator(mode="after")
    def validate_members(self) -> CreativeUnitContent:
        if len(set(self.ordered_material_ids)) != len(self.ordered_material_ids):
            raise ValueError("同一创意内不能重复引用素材")
        if self.kind == "VIDEO" and len(self.ordered_material_ids) != 1:
            raise ValueError("视频单元只能引用一个视频")
        if self.music_status == "READY" and self.music_id is None:
            raise ValueError("READY 创意必须选择音乐")
        if self.kind == "CAROUSEL" and self.music_status == "NOT_REQUIRED":
            raise ValueError("图片创意必须配置音乐")
        return self


class CreativeUnit(CreativeUnitContent):
    id: UUID


class ProposedCreativeUnit(CreativeUnitContent):
    # 不是 UUID 占位符或内容哈希身份；T2 持久化 plan 时分配一次。
    id: None = None


class ExcludedImage(ImmutableModel):
    material_id: UUID
    reason: Literal["DISABLED", "REMAINDER"]


class CompositionWarning(ImmutableModel):
    material_id: UUID
    reason: Literal["UPLOAD_SEQUENCE_MISSING"]


class CompositionConflict(ImmutableModel):
    code: Literal[
        "NO_IMAGES",
        "PROTECTED_MUSIC_COMPOSITION_CHANGED",
        "PROTECTED_IMAGE_UNAVAILABLE",
    ]
    unit_id: UUID | None = None
    material_ids: tuple[UUID, ...] = ()


class CompositionDiff(ImmutableModel):
    units: tuple[CreativeUnit | ProposedCreativeUnit, ...]
    retained_ids: tuple[UUID, ...]
    removed_ids: tuple[UUID, ...]
    excluded_images: tuple[ExcludedImage, ...]
    conflicts: tuple[CompositionConflict, ...]
    rule_digest: str
    candidate_digest: str
    warnings: tuple[CompositionWarning, ...] = ()
    protection_losses: tuple[UUID, ...] = ()
