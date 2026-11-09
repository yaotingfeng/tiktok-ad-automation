"""各维度共用的窄日指标；名称与归属仍由独立目录/成员证据提供。"""

from sqlalchemy import CheckConstraint, Index
from sqlmodel import Field

from app.modules.reporting.read_models.models import _ReadDay, account_reference


class EntityMetricDay(_ReadDay, table=True):
    __tablename__ = "entity_metric_day"
    __table_args__ = (
        account_reference(),
        CheckConstraint(
            "kind IN ('campaign','adgroup','ad','material')",
            name="ck_entity_metric_day_kind",
        ),
        CheckConstraint(
            "kind='material' OR variant=''", name="ck_entity_metric_day_variant"
        ),
        Index(
            "ix_entity_metric_day_period",
            "tenant_id",
            "kind",
            "stat_date",
            "advertiser_id",
        ),
        {"postgresql_partition_by": "RANGE (stat_date)"},
    )
    kind: str = Field(primary_key=True, max_length=16)
    # 素材使用完整 row_key，而不是可能同名/缺失的素材库 ID。
    remote_id: str = Field(primary_key=True, max_length=512)
    variant: str = Field(default="", primary_key=True, max_length=64)
