"""手工命令严格判别联合；复用与解除保护均必须是显式用户动作。"""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.modules.builds.image_composition_schemas import ImmutableModel, MusicId

ImageIds = Annotated[tuple[UUID, ...], Field(min_length=1, max_length=35)]


class SetImages(ImmutableModel):
    kind: Literal["SET_IMAGES"]
    unit_id: UUID
    material_ids: ImageIds
    allow_reuse: bool = False


class Create(ImmutableModel):
    kind: Literal["CREATE"]
    drama_id: UUID
    material_ids: ImageIds
    allow_reuse: bool = False


class Duplicate(ImmutableModel):
    kind: Literal["DUPLICATE"]
    unit_id: UUID


class Merge(ImmutableModel):
    kind: Literal["MERGE"]
    unit_ids: tuple[UUID, ...] = Field(min_length=2, max_length=35)


class Split(ImmutableModel):
    kind: Literal["SPLIT"]
    unit_id: UUID
    split_positions: tuple[int, ...] = Field(min_length=1, max_length=34)


class Delete(ImmutableModel):
    kind: Literal["DELETE"]
    unit_id: UUID


class SetProtection(ImmutableModel):
    kind: Literal["SET_PROTECTION"]
    unit_id: UUID
    composition_protected: bool | None = None
    music_protected: bool | None = None

    @model_validator(mode="after")
    def has_change(self):
        if self.composition_protected is None and self.music_protected is None:
            raise ValueError("请选择保护字段")
        return self


class RestoreCandidates(ImmutableModel):
    kind: Literal["RESTORE_CANDIDATES"]
    drama_id: UUID
    material_ids: tuple[UUID, ...] = Field(min_length=1, max_length=1000)


class SetMusic(ImmutableModel):
    kind: Literal["SET_MUSIC"]
    unit_id: UUID
    music_id: MusicId


CreativeEditCommand = Annotated[
    SetImages
    | Create
    | Duplicate
    | Merge
    | Split
    | Delete
    | SetProtection
    | RestoreCandidates
    | SetMusic,
    Field(discriminator="kind"),
]
