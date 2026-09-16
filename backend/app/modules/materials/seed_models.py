"""跨 BC 内容首次转存身份；状态由唯一实际分发任务保存。"""

from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint
from sqlmodel import Field, SQLModel

from .models import tenant_material_reference


class MaterialBCSeed(SQLModel, table=True):
    __tablename__ = "material_bc_seed"
    __table_args__ = (
        tenant_material_reference(),
        UniqueConstraint(
            "tenant_id",
            "bc_id",
            "content_key",
            "generation",
            name="uq_material_bc_seed_content",
        ),
        CheckConstraint("generation >= 1", name="ck_material_bc_seed_generation"),
        UniqueConstraint(
            "tenant_id", "bc_id", "id", name="uq_material_bc_seed_identity"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "bc_id", "material_id", "advertiser_id", "distribution_id"],
            [
                "material_distribution.tenant_id",
                "material_distribution.bc_id",
                "material_distribution.material_id",
                "material_distribution.advertiser_id",
                "material_distribution.id",
            ],
            name="fk_material_bc_seed_distribution",
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    bc_id: str = Field(max_length=128)
    material_id: UUID
    advertiser_id: str = Field(max_length=128)
    distribution_id: UUID
    content_key: str = Field(max_length=256)
    generation: int = Field(default=1)
