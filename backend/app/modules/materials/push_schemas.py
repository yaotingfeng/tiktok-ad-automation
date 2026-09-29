"""外部工具只提交业务身份、原名和 URL，媒体事实由后台核实。"""

from datetime import datetime
from pathlib import PurePosixPath
from typing import Self
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".webm", ".mpeg", ".3gp", ".mkv"}


class PushMaterialInput(BaseModel):
    model_config = {"extra": "forbid"}
    material_id: str = Field(min_length=1, max_length=255)
    file_name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=1, max_length=8192, repr=False)

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        for value in (self.material_id, self.file_name):
            if value != value.strip() or any(
                ord(c) < 32 or ord(c) == 127 for c in value
            ):
                raise ValueError("素材 ID 和名称不能包含控制字符或首尾空白")
        if (
            any(c in self.file_name for c in ("/", "\\"))
            or PurePosixPath(self.file_name).suffix.lower() not in VIDEO_EXTENSIONS
        ):
            raise ValueError("文件名必须带支持的视频扩展名且不能包含路径")
        return self


class PushBatchInput(BaseModel):
    model_config = {"extra": "forbid"}
    tenant_name: str = Field(min_length=1, max_length=120)
    materials: list[PushMaterialInput] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def unique_materials(self) -> Self:
        if self.tenant_name != self.tenant_name.strip():
            raise ValueError("租户名称必须精确匹配")
        if len({item.material_id for item in self.materials}) != len(self.materials):
            raise ValueError("同批素材 ID 不能重复")
        return self


class PushItemPublic(BaseModel):
    material_id: str
    revision: int
    file_name: str
    library_material_id: UUID | None
    status: str
    error_code: str | None = None
    advertiser_id: str | None = None
    video_id: str | None = None


class PushBatchPublic(BaseModel):
    batch_id: UUID
    request_id: UUID
    tenant_name: str
    bc_id: str
    status: str
    accepted_count: int
    created_at: datetime
    materials: list[PushItemPublic]
